#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Entry shim; the implementation lives in `agent_gateway.services.rss.rss_daily`."""

from agent_gateway.services.rss.rss_daily import main

if __name__ == "__main__":
    raise SystemExit(main())
