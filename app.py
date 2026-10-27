"""Festival page and the same-origin interest submission API."""

from contextlib import asynccontextmanager
from datetime import datetime, timezone
import logging
import os
import secrets
from typing import Annotated
from uuid import UUID
from asyncio import to_thread

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, ConfigDict, EmailStr, Field, StrictBool, field_validator
from sqlalchemy import func, or_, select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from database import ROOT, Interest, InterestChild, build_engine, initialize_database

logger = logging.getLogger(__name__)


def log_database_failure(exc: SQLAlchemyError, operation: str) -> None:
    original = getattr(exc, "orig", None)
    logger.error(
        "Database %s failed (%s; driver=%s; SQLSTATE=%s); check database access and schema permissions.",
        operation, type(exc).__name__,
        type(original).__name__ if original is not None else "unknown",
        getattr(original, "sqlstate", None) or "unknown",
    )


class InterestSubmission(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    submission_id: UUID
    contact_name: Annotated[str, Field(min_length=1, max_length=120)]
    contact_email: Annotated[EmailStr, Field(max_length=254)]
    children: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=120)]],
        Field(min_length=1, max_length=20),
    ]
    volunteer: StrictBool = False
    notes: Annotated[str, Field(max_length=2000)] = ""

    @field_validator("contact_email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return value.lower()


class AdminInterest(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    contact_name: str = Field(validation_alias="name")
    contact_email: str = Field(validation_alias="email")
    children: list[str]
    volunteer: bool
    notes: str
    submitted_at: datetime

    @field_validator("children", mode="before")
    @classmethod
    def child_names(cls, value):
        return [child.name if isinstance(child, InterestChild) else child for child in value]

    @field_validator("submitted_at")
    @classmethod
    def include_timestamp_timezone(cls, value: datetime) -> datetime:
        # SQLite's CURRENT_TIMESTAMP is UTC but returns a naive datetime.
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


class AdminInterestList(BaseModel):
    total: int
    total_children: int
    total_volunteers: int
    matched: int
    page: int
    page_size: int
    items: list[AdminInterest]


def create_app(database_url: str | None = None) -> FastAPI:
    engine = build_engine(database_url)
    admin_username = os.getenv("ADMIN_USERNAME") or "admin"
    admin_password = os.getenv("ADMIN_PASSWORD", "")
    admin_security = HTTPBasic(auto_error=False, realm="Voices of the Valley admin")

    def require_admin(
        credentials: Annotated[HTTPBasicCredentials | None, Depends(admin_security)],
    ):
        if not admin_password:
            raise HTTPException(503, "Admin access has not been configured.")
        challenge = {"WWW-Authenticate": 'Basic realm="Voices of the Valley admin"'}
        if credentials is None:
            raise HTTPException(401, "Admin sign-in required.", headers=challenge)
        username_matches = secrets.compare_digest(
            credentials.username.encode("utf-8"), admin_username.encode("utf-8")
        )
        password_matches = secrets.compare_digest(
            credentials.password.encode("utf-8"), admin_password.encode("utf-8")
        )
        if not (username_matches and password_matches):
            raise HTTPException(401, "Incorrect username or password.", headers=challenge)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        try:
            try:
                # Finish schema setup before serving requests on either backend.
                # The initializer serializes concurrent starts at the database.
                await to_thread(initialize_database, engine)
            except SQLAlchemyError as exc:
                log_database_failure(exc, "initialization")
                raise
            logger.info("Database schema ready.")
            yield
        finally:
            engine.dispose()

    application = FastAPI(title="Voices of the Valley", lifespan=lifespan)
    application.state.engine = engine

    @application.middleware("http")
    async def protect_admin_responses(request: Request, call_next):
        response = await call_next(request)
        if request.url.path in {"/admin", "/admin/"} or request.url.path.startswith("/api/admin/"):
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Robots-Tag"] = "noindex, nofollow"
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Content-Security-Policy"] = (
                "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
            )
        return response

    @application.exception_handler(RequestValidationError)
    async def invalid_submission(request: Request, exc: RequestValidationError):
        # Return field names and messages without echoing submitted personal data.
        return JSONResponse(
            status_code=422,
            content={"detail": [
                {"field": str(error["loc"][1]) if len(error["loc"]) > 1 else str(error["loc"][0]),
                 "path": list(error["loc"][1:]), "message": error["msg"]}
                for error in exc.errors()
            ]},
            headers={"Cache-Control": "no-store"},
        )

    @application.exception_handler(SQLAlchemyError)
    async def database_unavailable(request: Request, exc: SQLAlchemyError):
        log_database_failure(exc, "request")
        message = (
            "We couldn't load registrations. Please try again shortly."
            if request.url.path.startswith("/api/admin/")
            else "We couldn't save your interest. Please try again shortly."
        )
        return JSONResponse(
            status_code=503,
            content={"detail": message},
            headers={"Cache-Control": "no-store", "Retry-After": "30"},
        )

    @application.get("/", include_in_schema=False)
    @application.get("/index.html", include_in_schema=False)
    def index():
        return FileResponse(ROOT / "index.html", media_type="text/html")

    @application.get("/admin", dependencies=[Depends(require_admin)], include_in_schema=False)
    @application.get("/admin/", dependencies=[Depends(require_admin)], include_in_schema=False)
    def admin_portal():
        return FileResponse(ROOT / "admin.html", media_type="text/html")

    @application.get(
        "/api/admin/interests",
        dependencies=[Depends(require_admin)],
        response_model=AdminInterestList,
    )
    def registered_interest(
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 50,
        q: Annotated[str, Query(max_length=120)] = "",
    ):
        query = select(Interest)
        count_query = select(func.count()).select_from(Interest)
        search = q.strip()
        if search:
            matches = or_(
                Interest.name.icontains(search, autoescape=True),
                Interest.email.icontains(search, autoescape=True),
                Interest.notes.icontains(search, autoescape=True),
                Interest.children.any(InterestChild.name.icontains(search, autoescape=True)),
            )
            query = query.where(matches)
            count_query = count_query.where(matches)
        with Session(engine) as session:
            total = session.scalar(select(func.count()).select_from(Interest))
            total_children = session.scalar(select(func.count()).select_from(InterestChild))
            total_volunteers = session.scalar(
                select(func.count()).select_from(Interest).where(Interest.volunteer.is_(True))
            )
            matched = session.scalar(count_query)
            page = min(page, max(1, (matched + page_size - 1) // page_size))
            records = session.scalars(
                query.order_by(Interest.submitted_at.desc(), Interest.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
            return AdminInterestList(
                total=total,
                total_children=total_children,
                total_volunteers=total_volunteers,
                matched=matched,
                page=page,
                page_size=page_size,
                items=[AdminInterest.model_validate(record) for record in records],
            )

    @application.get("/api/health")
    def health(response: Response):
        response.headers["Cache-Control"] = "no-store"
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            # Also confirm schema initialization, without reading personal data.
            connection.execute(select(Interest.id, Interest.volunteer).limit(0))
            connection.execute(select(InterestChild.id).limit(0))
        return {"status": "ok"}

    @application.post("/api/interests", status_code=201)
    def submit_interest(payload: InterestSubmission, response: Response):
        response.headers["Cache-Control"] = "no-store"
        values = {
            "name": payload.contact_name,
            "email": payload.contact_email,
            "choir": "multiple" if len(payload.children) > 1 else "children",
            "notes": payload.notes,
            "notify": False,
            "volunteer": payload.volunteer,
        }
        with Session(engine) as session:
            session.add(Interest(
                id=payload.submission_id, **values,
                children=[InterestChild(name=name, position=index) for index, name in enumerate(payload.children)],
            ))
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                existing = session.get(Interest, payload.submission_id)
                if existing is None:
                    raise
                if (any(getattr(existing, key) != value for key, value in values.items())
                        or [child.name for child in existing.children] != payload.children):
                    raise HTTPException(409, "Please refresh the page and submit again.")
                # A retry after a dropped response must not create another entry.
                response.status_code = 200
        return {"status": "recorded"}

    return application


app = create_app()
