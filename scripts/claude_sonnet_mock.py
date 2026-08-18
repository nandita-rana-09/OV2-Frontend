#!/usr/bin/env python3
"""Seed a mock Claude Sonnet 5 model into an Open WebUI database.

No Anthropic API key exists yet (see PROJECT_SOT.md 3), so this does NOT talk
to Anthropic at all. It registers a "pipe" Function -- Open WebUI's supported
way to add a model backed by arbitrary Python instead of a live API
connection (backend/open_webui/functions.py:get_function_models) -- whose
pipe() returns a fixed placeholder string. That makes the entry selectable in
the model dropdown and sendable in a chat, so the UI flow (select model, send
message, see a response) can be exercised without any credentials.

It also registers the existing MCP tool server (same URL already configured
as Claude Code's own "project-mcp" connection -- see `claude mcp get
project-mcp`) as an Open WebUI Tool Server, and attaches it to the mock
model. This is DB state, matching how scripts/cruz_migrate.py already
registers `eoxs-db` -- see that script's step [5] for the identical pattern.

The model id is `claude-sonnet-5-mock`, deliberately NOT `claude-sonnet-5`:
once a real Anthropic connection is added, Anthropic's own API will return a
live model literally named `claude-sonnet-5`, and reusing that id here would
collide with it. Delete this mock (or leave it inactive) at that point --
see the printed summary this script ends with.

No API key of any kind is read, written, or required.

    python scripts/claude_sonnet_mock.py                      # dry run
    python scripts/claude_sonnet_mock.py --apply
    PROJECT_MCP_URL='...' python scripts/claude_sonnet_mock.py --apply   # also wire MCP

Idempotent: safe to run repeatedly. Everything is done in ONE transaction, so
a failure part-way leaves the database untouched.
"""

import argparse
import json
import os
import sqlite3
import time

MOCK_MODEL_ID = 'claude-sonnet-5-mock'
MCP_SERVER_ID = 'project-mcp'  # matches the name used for Claude Code's own MCP connection

PIPE_SOURCE = '''"""
title: Claude Sonnet 5 (Mock)
author: OV2-Frontend
description: Placeholder standing in for the real Anthropic claude-sonnet-5 connection until an API key is configured. Returns a fixed response so the model-selection and MCP-attachment UI flow can be exercised end to end, with no live API calls.
version: 0.1.0
"""

from pydantic import BaseModel


class Pipe:
    class Valves(BaseModel):
        pass

    def __init__(self):
        self.id = "claude-sonnet-5-mock"
        self.name = "Claude Sonnet 5 (Mock)"

    def pipe(self, body: dict, __tools__: dict | None = None) -> str:
        user_message = ""
        for message in reversed(body.get("messages", [])):
            if message.get("role") == "user":
                user_message = message.get("content", "")
                break

        tool_names = list((__tools__ or {}).keys())
        if tool_names:
            tools_line = f"{len(tool_names)} MCP tool(s) visible: {', '.join(tool_names)}"
        else:
            tools_line = (
                "0 MCP tools resolved. The registered MCP server uses SSE transport; "
                "this build's MCP client requires streamable HTTP -- see PROJECT_SOT.md, "
                "Open issue #1. The tool server is still registered and attached; only "
                "live tool-spec fetching is blocked by that transport mismatch."
            )

        return (
            "**[Mock response \\u2014 no Anthropic API key configured]**\\n\\n"
            f"You said: \\u201c{user_message}\\u201d\\n\\n"
            "This is a placeholder standing in for a real call to Claude Sonnet 5 "
            "(claude-sonnet-5) via the Anthropic API. Add a real Anthropic connection "
            "under Admin Settings \\u2192 Connections to get real responses from this model.\\n\\n"
            f"MCP status: {tools_line}"
        )
'''

log = []


def note(msg):
    log.append(msg)
    print('  ' + msg)


def jload(v, default):
    try:
        return json.loads(v) if v else default
    except Exception:
        return default


def migrate(db_path, apply, mcp_url):
    c = sqlite3.connect(db_path)
    c.execute('BEGIN')
    try:
        _run(c, mcp_url)
    except Exception:
        c.rollback()
        c.close()
        raise
    if apply:
        c.commit()
        print('\nCOMMITTED')
    else:
        c.rollback()
        print('\nDRY RUN -- nothing written. Re-run with --apply.')
    c.close()


def _run(c, mcp_url):
    now = int(time.time())
    admin = list(c.execute("select id from user where role='admin' limit 1"))
    if not admin:
        raise SystemExit('no admin user found -- sign in to Open WebUI once first, then re-run')
    admin = admin[0][0]

    print('\n[1] mock pipe function')
    if list(c.execute('select 1 from function where id=?', (MOCK_MODEL_ID,))):
        note(f'{MOCK_MODEL_ID} already registered')
    else:
        c.execute(
            'insert into function (id,user_id,name,type,content,meta,valves,is_active,is_global,updated_at,created_at) '
            'values (?,?,?,?,?,?,?,1,0,?,?)',
            (
                MOCK_MODEL_ID,
                admin,
                'Claude Sonnet 5 (Mock)',
                'pipe',
                PIPE_SOURCE,
                json.dumps({'description': 'Placeholder for claude-sonnet-5 -- no Anthropic API key configured yet.'}),
                None,
                now,
                now,
            ),
        )
        note(f'{MOCK_MODEL_ID} created and activated')

    print('\n[2] MCP tool server')
    if not mcp_url:
        note('SKIPPED -- no MCP URL given (set PROJECT_MCP_URL or pass --mcp-url)')
        tool_ids = []
    else:
        row = list(c.execute("select value from config where key='tool_server.connections'"))
        conns = jload(row[0][0], []) if row else []
        if any(x.get('info', {}).get('id') == MCP_SERVER_ID for x in conns):
            note(f'{MCP_SERVER_ID} already registered')
        else:
            conns.append({
                'url': mcp_url, 'path': '', 'type': 'mcp', 'auth_type': 'none',
                'headers': None, 'key': None,
                'config': {'enable': True, 'access_grants': []},
                'info': {'id': MCP_SERVER_ID, 'name': 'Project MCP',
                         'description': 'Reuses the same MCP server already connected as Claude Code\'s "project-mcp".'},
            })
            c.execute(
                "update config set value=? where key='tool_server.connections'" if row
                else "insert into config (key,value,updated_at) values ('tool_server.connections',?,?)",
                (json.dumps(conns),) if row else (json.dumps(conns), now),
            )
            note(f'{MCP_SERVER_ID} registered (url reused verbatim, not modified)')
        tool_ids = [f'server:mcp:{MCP_SERVER_ID}']

    print('\n[3] workspace model override (name, description, MCP attachment)')
    row = list(c.execute('select meta from model where id=?', (MOCK_MODEL_ID,)))
    if row:
        note(f'{MOCK_MODEL_ID} override already exists')
    else:
        meta = {
            'description': 'Placeholder for Claude Sonnet 5. No Anthropic API key configured -- '
                            'returns a fixed mock response. See PROJECT_SOT.md 3.',
        }
        if tool_ids:
            meta['toolIds'] = tool_ids
        c.execute(
            'insert into model (id,user_id,base_model_id,name,params,meta,updated_at,created_at,is_active) '
            'values (?,?,?,?,?,?,?,?,1)',
            (
                MOCK_MODEL_ID, admin, None, 'Claude Sonnet 5 (Mock)',
                json.dumps({}), json.dumps(meta), now, now,
            ),
        )
        note(f'{MOCK_MODEL_ID} override created' + (' with MCP tools attached' if tool_ids else ''))

    print('\n[4] final model visibility')
    for i, n, a in c.execute('select id,name,is_active from model order by id'):
        print(f'  {"VISIBLE" if a else "inactive":9} {n} ({i})')


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default='backend/data/webui.db')
    ap.add_argument('--apply', action='store_true', help='write changes (default is a dry run)')
    ap.add_argument('--mcp-url', default=os.environ.get('PROJECT_MCP_URL', ''),
                     help='MCP server URL to register (or set PROJECT_MCP_URL env var). '
                          'Never hardcoded here -- omit to skip MCP wiring entirely.')
    a = ap.parse_args()
    print(f'database: {a.db}')
    migrate(a.db, a.apply, a.mcp_url.strip())
