from sqlalchemy import Float, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAt, UuidPk


class BenchmarkRun(Base):
    __tablename__ = "benchmark_runs"

    id: Mapped[UuidPk]
    model_name: Mapped[str] = mapped_column(String(255))
    quantization: Mapped[str | None] = mapped_column(String(64))  # e.g. "awq"; null for API
    avg_latency_ms: Mapped[float] = mapped_column(Float)
    throughput_tokens_per_sec: Mapped[float] = mapped_column(Float)
    cost_estimate: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[CreatedAt]
