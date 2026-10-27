"""Start the local festival app with: python run.py."""

from pathlib import Path

import uvicorn


if __name__ == "__main__":
    project_dir = str(Path(__file__).resolve().parent)
    uvicorn.run(
        "app:app",
        host="127.0.0.1",
        port=8001,
        reload=True,
        app_dir=project_dir,
        reload_dirs=[project_dir],
    )
