from fastapi import FastAPI
from fastapi.responses import FileResponse
from app.dashboard.api import router
import os

app = FastAPI(title="Polymarket Bot v2")
app.include_router(router)


@app.get("/")
def home():
    return FileResponse(os.path.join(os.path.dirname(__file__), "index.html"))
