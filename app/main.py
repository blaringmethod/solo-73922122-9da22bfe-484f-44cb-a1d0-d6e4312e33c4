"""FastAPI application for the property-outage daily report.

Endpoints
---------
POST /days/{day}/topology          register a UTC day (buildings, power sources, branches)
POST /days/{day}/branches/{branch}/corrections
                                   replace one branch's whole-day record (version-checked)
GET  /days/{day}/report            read the current daily report (merged intervals + version)
GET  /days/{day}/report?version=N  read a historical report version
GET  /days/{day}/versions          list report versions, marking the current one
GET  /days/{day}/topology          inspect the registered topology
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Query, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import service
from .db import init_db
from .errors import ApiError
from .schemas import (
    CorrectionIn,
    DayRegistration,
    ReportOut,
    ReportVersionsOut,
    TopologyOut,
)

DB_PATH = os.environ.get("OUTAGE_DB_PATH", "data/outage.db")

# uvicorn configures its own loggers; make sure the service logger's INFO
# messages (report branch counts and elapsed time) are visible in `docker logs`.
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")


def _status_payload(report: ReportOut) -> dict[str, Any]:
    return {
        "day": report.day,
        "version": report.version,
        "generated_at": report.generated_at,
        "confirmed_outage": [b.code for b in report.buildings if b.status == "confirmed_outage"],
        "needs_verification": [b.code for b in report.buildings if b.status == "needs_verification"],
    }


def create_app(db_path: str | None = None) -> FastAPI:
    path = db_path or os.environ.get("OUTAGE_DB_PATH", DB_PATH)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        init_db(path)
        yield

    app = FastAPI(
        title="物业停电日报核算 API",
        version="1.0.0",
        lifespan=lifespan,
        description=(
            "按 UTC 日登记楼栋、全天带电电源点、双向支路及支路导通/断开时段，"
            "生成每栋楼的已确认停电与待核实时段，并支持凭当前版号整日替换单条支路。"
        ),
    )
    app.state.db_path = path

    @app.exception_handler(ApiError)
    async def api_error_handler(_request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"error": exc.detail()})

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_error",
                    "message": "request body or query parameters are invalid",
                    "details": jsonable_encoder(exc.errors()),
                }
            },
        )

    @app.get("/health", tags=["meta"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post(
        "/days/{day}/topology",
        response_model=ReportOut,
        status_code=201,
        tags=["registration"],
        summary="登记某个 UTC 日的楼栋、电源点与支路，并生成第 1 版日报",
    )
    def register_day(day: str, body: DayRegistration) -> ReportOut:
        return service.register_day(path, day, body)

    @app.get(
        "/days/{day}/topology",
        response_model=TopologyOut,
        tags=["registration"],
        summary="查看某日已登记的拓扑",
    )
    def get_topology(day: str) -> TopologyOut:
        return service.get_topology(path, day)

    @app.get(
        "/days/{day}/report",
        response_model=ReportOut,
        tags=["report"],
        summary="按日读取已生成的日报（默认当前版，可指定历史版）",
    )
    def get_report(
        day: str,
        version: int | None = Query(default=None, ge=1, description="省略则读取当前版"),
    ) -> ReportOut:
        return service.get_report(path, day, version)

    @app.get(
        "/days/{day}/versions",
        response_model=ReportVersionsOut,
        tags=["report"],
        summary="列出版本号及当前版标记",
    )
    def list_versions(day: str) -> ReportVersionsOut:
        return service.list_report_versions(path, day)

    @app.post(
        "/days/{day}/branches/{branch_code}/corrections",
        response_model=ReportOut,
        tags=["correction"],
        summary="凭当前版号整日替换一条支路的记录，并原子地生成新版日报",
    )
    def correct_branch(
        day: str, branch_code: str, body: CorrectionIn, response: Response
    ) -> ReportOut:
        new_version, report = service.correct_branch(path, day, branch_code, body)
        response.headers["X-Report-Version"] = str(new_version)
        return report

    @app.get(
        "/days/{day}/summary",
        tags=["report"],
        summary="便捷汇总：当前版号、确认停电楼栋与待核实楼栋",
    )
    def summary(day: str) -> dict[str, Any]:
        return _status_payload(service.get_report(path, day))

    return app


app = create_app()
