from fastapi import FastAPI

app = FastAPI(title="Agentic Dev/Test Copilot")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
