# -*- coding: utf-8 -*-
"""quantweb — 量化分析网站的 FastAPI 适配层。

架构:
  React 前端 (dashboard/, 移植自 量化1/AStock-Quant-Lab-V2)
  └─ /api/* 契约 (量化1 的 7 页看板接口)
     └─ quantweb.app → immutable SQLite 只读查询仓库
        ├─ V2 可写数据仅在 data/v2
        └─ data/v2/legacy_archive/published 作为主项目内历史证据归档

安全边界: LOCAL_ONLY、无券商/账户/鉴权、无实盘订单；读取不执行DDL。
"""

__version__ = "1.0.0"
