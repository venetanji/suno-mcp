#!/bin/bash
export DISPLAY=:99
until xdpyinfo >/dev/null 2>&1; do
    sleep 0.1
done
exec openbox
