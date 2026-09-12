#!/usr/bin/env bash
# Default: browser-assisted trader-name import, no clipboard or token export.
# --console / --print retain the optional manual JWT diagnostic helper.
set -euo pipefail

# The default now collects/imports names, rather than only exporting a JWT.
# Keep the old console path available explicitly for manual diagnostics.
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONSOLE=0
for arg in "$@"; do
  case "$arg" in --print|--console) CONSOLE=1 ;; esac
done
if [ "$CONSOLE" -eq 0 ]; then
  exec "$ROOT/.venv/bin/python" "$ROOT/scripts/fomo_sync.py" "$@"
fi

BASE="${FOMO_API_BASE:-http://127.0.0.1:8000}"
PRINT_ONLY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --console) ;;
    --print) PRINT_ONLY=1 ;;
    --base) shift; BASE="${1:?--base requires a URL}" ;;
    --base=*) BASE="${1#--base=}" ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done
BASE="${BASE%/}"

# Manual fallback only: read a cached access token, never rotate refresh tokens.
SNIPPET=$(cat <<'JS'
async function fomoToken(){
  const unwrap=v=>{if(v==null)return null;try{const p=JSON.parse(v);return typeof p==="string"?p:((p&&p.token)||v);}catch(e){return v;}};
  const fresh=t=>{try{const c=JSON.parse(atob(t.split(".")[1].replace(/-/g,"+").replace(/_/g,"/")));return c.exp*1000>Date.now()+60000;}catch(e){return false;}};
  let t=unwrap(localStorage.getItem("privy:token"));
  if(t&&fresh(t))return t;
  return null;
}
(async()=>{
  const jwt=(await fomoToken())||prompt("Вставьте FOMO Bearer-токен (без слова Bearer)");
  if(!jwt){console.warn("FOMO: токен не получен");return;}
  try{
    const r=await fetch("__BASE__/api/fomo/session",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({jwt})});
    if(r.ok){const s=await r.json();console.log("%cFOMO: сессия передана в localhost"+(s.expires_in?" (~"+Math.round(s.expires_in/60)+" мин)":""),"color:#58d68d");return;}
    throw new Error("HTTP "+r.status);
  }catch(e){
    try{await navigator.clipboard.writeText(jwt);console.log("%cFOMO: мост выключен — токен скопирован, вставьте его в поле на странице /fomo","color:#f2c66d");}
    catch(e2){console.warn("FOMO: скопируйте токен вручную из поля выше");}
  }
})();
JS
)
SNIPPET="${SNIPPET//__BASE__/$BASE}"

if [ "$PRINT_ONLY" -eq 1 ]; then
  printf '%s\n' "$SNIPPET"
  exit 0
fi

if command -v pbcopy >/dev/null 2>&1; then
  # pbcopy re-encodes its input per LC_CTYPE, and a plain "C" locale (the
  # default in a non-login shell) turns the Russian strings into Mac OS Roman
  # mojibake by the time they reach the console. Pin UTF-8 for this pipe only.
  printf '%s' "$SNIPPET" | LC_CTYPE=UTF-8 pbcopy
  echo "Сниппет скопирован в буфер обмена."
else
  printf '%s\n\n' "$SNIPPET"
  echo "(pbcopy недоступен — скопируйте сниппет выше вручную.)"
fi

cat <<EOF
Дальше, во вкладке fomo.family (вы должны быть залогинены):
  1. Откройте консоль (Cmd+Opt+J), при первой вставке введите: allow pasting
  2. Вставьте сниппет и нажмите Enter.
Токен будет прочитан из текущей сессии (либо запрошен вручную) и передан на ${BASE}
(если включён FOMO_TOKEN_BRIDGE) либо скопирован для вставки в поле на /fomo.
EOF

if command -v open >/dev/null 2>&1; then
  open "https://fomo.family" >/dev/null 2>&1 || true
fi
