#!/bin/sh
# Opens the wireframer in your default browser. Fully offline; nothing is sent anywhere.
f="$(cd "$(dirname "$0")" && pwd)/index.html"
(xdg-open "$f" || open "$f") >/dev/null 2>&1
