"""业务路由汇总：工作流固定路径先注册，通用资源路由后注册。"""
from __future__ import annotations

from app.routers import resource, workflow

ROUTERS = [workflow, resource]
