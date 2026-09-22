#!/bin/bash
cd /tmp/work/prj_01M34JT3JNQTTMG6GKRD6HPM8V/batchtruss
rm -f /tmp/matrix.log
for m in batch batch-nomerge peredge; do
  ok=0; bad=0
  for g in synth1 synth2; do
    s=s1; [ $g = synth2 ] && s=s2
    for f in data/${s}_b*_p*.txt; do
      case "$f" in *b1000*) ce=50;; *b100*) ce=25;; *) ce=1;; esac
      [ "$m" = "peredge" ] || ce=1
      r=$(timeout 1800 ./debug_bisect --graph data/$g.txt --stream $f --method $m --check-every $ce 2>/dev/null | tail -1)
      echo "$m $g $(basename $f) ce=$ce: $r" >> /tmp/matrix.log
      case "$r" in "ALL OK"*) ok=$((ok+1));; *) bad=$((bad+1));; esac
    done
  done
  echo "== $m: ok=$ok bad=$bad" >> /tmp/matrix.log
done
echo "MATRIX DONE" >> /tmp/matrix.log
