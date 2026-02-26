from fastapi import FastAPI

app = FastAPI(title="Factory", description="Agent farm orchestrator")


@app.get("/health")
async def health():
    return {"status": "ok"}
