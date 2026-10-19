#!/usr/bin/env bash
# Starts the Python Notion bridge with this machine's Notion session.
# The cookie is read from Magpie's stored login and never printed.
set -e
AUTH="C:/Users/li/.config/magpie/plugin-auth.json"
TOKEN=$(./.venv/Scripts/python.exe -c "
import json
d = json.load(open(r'$AUTH', encoding='utf-8'))
print(d['notion#ungy5k']['key'])
")
export NOTION_COOKIE="token_v2=$TOKEN"
export NOTION_IMPERSONATE=chrome150
export NOTIONCHAT_API_KEY=localtest
export NOTIONCHAT_PORT=1994
exec ./.venv/Scripts/python.exe -m notionchat serve
