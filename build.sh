#!/bin/bash
# Convert the books in BOOKS, build it with Antora, compare with the DocBook source.
set -euo pipefail
POC=$(cd "$(dirname "$0")" && pwd)
SRC=${SRC:-$POC/../doc-sle}
ANTORA=${ANTORA:-$POC/../../product-docs/product-docs-playbook/node_modules/.bin/antora}
STAGE=/tmp/adoc-sle-poc-stage

BOOKS=(book_rmt.xml:rmt book_gnome-user.xml:gnome-user book_administration.xml:administration)
python3 "$POC/tools/db2adoc.py" --src "$SRC" --out "$POC/docs" \
  $(for b in "${BOOKS[@]}"; do printf -- '--book %s ' "${b%%:*}"; done)

rm -rf "$STAGE" && mkdir -p "$STAGE"
cp -r "$POC/docs/." "$STAGE/"
git -C "$STAGE" init -q
git -C "$STAGE" add -A
git -C "$STAGE" -c user.name=stage -c user.email=stage@localhost commit -q -m stage

cd "$POC"
rm -rf build
"$ANTORA" --stacktrace antora-playbook.yml 2>&1 | tee reports/antora.log

rm -f reports/compare-*.json
for b in "${BOOKS[@]}"; do
  python3 "$POC/tools/compare.py" --src "$SRC" --site "$POC/build/site" \
    --docs "$POC/docs" --out "$POC/reports" --book "${b%%:*}" --module "${b##*:}"
done
