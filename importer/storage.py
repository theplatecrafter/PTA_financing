from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SAVING_DIR = ROOT / "saving"


def ensure_saving_dir() -> Path:
    SAVING_DIR.mkdir(parents=True, exist_ok=True)
    return SAVING_DIR
