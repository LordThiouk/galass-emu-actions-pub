#!/usr/bin/env bash
# Lancé par android-emulator-runner (qui exécute 'script:' ligne par ligne et casse les '\').
set +e
flutter pub get >/dev/null
adb emu geo fix -17.4677 14.7167
adb logcat -c || true
adb logcat -v time > logcat.txt 2>&1 &
LOGCAT_PID=$!
timeout 1500 patrol test -t patrol_test/rider_home_destination_test.dart --verbose \
  --dart-define=API_BASE_URL="$BASE" \
  --dart-define=RIDER_TEST_PHONE="$RIDER_TEST_PHONE" \
  --dart-define=RIDER_TEST_OTP="$RIDER_TEST_OTP" 2>&1 \
  | tee patrol-test.log \
  | sed -E 's#https?://[^ ]+#<url>#g' \
  | tail -n 200
rc=${PIPESTATUS[0]}
echo "patrol test exit code: ${rc}"
kill "$LOGCAT_PID" 2>/dev/null
wait "$LOGCAT_PID" 2>/dev/null
exit "$rc"
