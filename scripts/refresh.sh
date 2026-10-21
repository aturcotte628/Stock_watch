#!/bin/bash
# Daily data refresh: daily bars, then hourly, then 5-minute. Run from cron.
cd "$(dirname "$0")/.." || exit 1
PY=${PYTHON:-python3}
LOG=data/refresh.log
mkdir -p data
echo "==== $(date) refresh start ====" >> $LOG
$PY load_history.py --refresh >> $LOG 2>&1
$PY load_history.py --refresh --interval 1h --workers 8 >> $LOG 2>&1
$PY load_history.py --refresh --interval 5m --workers 8 >> $LOG 2>&1
echo "==== $(date) refresh done ====" >> $LOG
