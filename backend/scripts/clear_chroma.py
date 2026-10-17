"""清空 ChromaDB 中的 travel_guides collection。"""

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.config import CHROMA_DB_DIR, CHROMA_COLLECTION_NAME

import chromadb

client = chromadb.PersistentClient(path=str(CHROMA_DB_DIR))
try:
    client.delete_collection(CHROMA_COLLECTION_NAME)
    print(f"已删除 collection: {CHROMA_COLLECTION_NAME}")
except Exception:
    print(f"collection {CHROMA_COLLECTION_NAME} 不存在，无需删除。")
