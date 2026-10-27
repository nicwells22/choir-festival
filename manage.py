"""Private database administration from a trusted local terminal."""

import argparse
import csv

from sqlalchemy import select
from sqlalchemy.orm import Session

from database import Interest, build_engine, initialize_database


def spreadsheet_safe(value: object) -> str:
    text = str(value)
    if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")):
        return "'" + text
    return text


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init-db", help="Create or upgrade tables; preserve existing records.")
    export = commands.add_parser("export-interests", help="Export submissions to a private CSV.")
    export.add_argument("--output", required=True)
    export.add_argument("--volunteers-only", action="store_true")
    args = parser.parse_args()
    engine = build_engine()
    try:
        if args.command == "init-db":
            initialize_database(engine)
            print("Database initialized.")
        else:
            query = select(Interest).order_by(Interest.submitted_at, Interest.id)
            if args.volunteers_only:
                query = query.where(Interest.volunteer.is_(True))
            fields = ["id", "contact_name", "contact_email", "children", "volunteer", "notes", "submitted_at"]
            with Session(engine) as session, open(args.output, "x", newline="", encoding="utf-8-sig") as output:
                writer = csv.writer(output)
                writer.writerow(fields)
                count = 0
                for interest in session.scalars(query):
                    values = [interest.id, interest.name, interest.email,
                              "\n".join(child.name for child in interest.children),
                              interest.volunteer, interest.notes, interest.submitted_at]
                    writer.writerow([spreadsheet_safe(value) for value in values])
                    count += 1
            print(f"Exported {count} submissions.")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
