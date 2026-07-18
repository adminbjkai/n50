#!/bin/bash
cd /Users/m17/2026/notion50/notion50new-v4 || exit 1
mkdir -p _tmp/ytdesc
IDS="JKQwLV3MBMM lPZrLtM3JBI CTs30iVcaEQ 3yt2thJoH98 TFRXkUECTcM AVWk63cfMgg wBMLiN-fclo 35A-g-Cs-cg afCTOG-aEt4 2m8dHR0Kw1I ZPwv5Ipqyzc dmrMH3FaQDo DlwsYrtZFas IUYUBlhEAqo 6Tcg8MjnBi0 48-lCMO4sQY 4D6bAf9BSIo 2cdpYJzPXWE BL_fTJc98U8 hGthy2e1Yn8 fPrSypBoWuA IncUYEqMZ_A JGBcwuPCR4c I41WFsHIlak a-6VY0i9C3Q 3DvmnCKlxdQ mYU9vcvL-yk"

one() {
  id="$1"
  yt-dlp --skip-download --no-warnings --print "%(title)s" "https://www.youtube.com/watch?v=$id" >"_tmp/ytdesc/$id.title" 2>/dev/null
  yt-dlp --skip-download --no-warnings --print "%(description)s" "https://www.youtube.com/watch?v=$id" >"_tmp/ytdesc/$id.desc" 2>/dev/null
  echo "fetched $id ($(grep -ciE 'github\.com/' "_tmp/ytdesc/$id.desc" 2>/dev/null) gh lines)"
}

n=0
for id in $IDS; do
  one "$id" &
  n=$((n+1))
  if [ $((n % 5)) -eq 0 ]; then wait; fi   # 5 at a time
done
wait
echo "DONE: $(ls _tmp/ytdesc/*.desc 2>/dev/null | wc -l | tr -d ' ') descriptions"
