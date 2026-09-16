"""Re-score a date. Default: today CT."""
from gap import clock, score
import sys
date = sys.argv[1] if len(sys.argv) > 1 else clock.today_ct()
print(score.score_date(date))
