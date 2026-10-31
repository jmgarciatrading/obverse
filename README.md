# Obverse

`Obverse.pine` is a TradingView Pine v6 overlay. It walks one execution sequence and reports that sequence on the dashboard.

The chart stays clear. Intermediate order blocks, gaps, and swing levels are not drawn. The MTF candle that sets the direction draws BUY under its low, or SELL over its high. The full sequence can also draw its own BUY or SELL label when it confirms.

## Timeframe stacks

| HTF period | MTF close-through, array, and inversion | LTF retest and MSS/BOS |
| --- | --- | --- |
| 4H | 15M | 5M |
| 4H | 5M | 1M |
| 1H | 15M | 5M |
| 1H | 5M | 1M |

Any other combination stops the script. The chart timeframe must be the selected LTF or lower, so every LTF candle is visible. MTF expiration still counts completed MTF candles, not chart candles.

## Sequence

`WAITING → BIAS → ARRAY → INVERSION → RETEST → MSS/BOS → CONFIRMED`

Bias and direction are separate. The dashboard has a row for each.

- **WAITING:** the two closed HTF candles did not produce a bias.
- **BIAS:** the new HTF candle has a bias, and direction is still empty. The sequence stays here until an MTF close through opposing candles sets the direction.
- **ARRAY:** direction is set and the period is being searched for one opposing MTF order block or fair value gap.
- **INVERSION:** the array is stored and later MTF closes are compared with its original open.
- **RETEST:** an MTF close has gone through that open. Later LTF candles can tag the original zone.
- **MSS/BOS:** an LTF candle has tagged the zone. A later LTF close must break the latest confirmed swing.
- **CONFIRMED:** that break has closed. The sequence stays here until the next HTF candle opens. A sweep-qualified opposing close-through before that returns the sequence to ARRAY.

Completed stages are green, the current stage is yellow, and later stages are gray. The execution-stage row uses the same stage value as the progress bar.

## Detection

At each new HTF open, bias is read from the two candles that have already closed. At 10:00 on the 1H, that is the 9:00 close compared with the 8:00 candle. The same comparison is used when the HTF is 4H.

- Bearish: the closed candle finished below the prior low, or it swept the prior high and closed back inside that range.
- Bullish: the closed candle finished above the prior high, or it swept the prior low and closed back inside that range.
- The sweep is strict. A close exactly on the swept extreme is not a rejection.
- If both readings are true, or neither is, the bias is None.
- A green body is still bearish when the close is below the prior low.

That reading is stored as the locked bias. The dashboard appends `flip` while the working bias disagrees with it. The next HTF open reads the bias again and clears the flip label.

Direction comes from the selected MTF. It does not use swing pivots. A bullish order block is a bullish candle that closes strictly above one or more earlier candles, and at least one of those candles is bearish. A bearish order block is the mirror: a bearish close strictly below one or more earlier candles, including a bullish one. The walk stops at the first candle the close does not clear. Candles beyond that one are not part of the order block, and they do not cancel the candles already cleared. A doji does not count as the opposing candle. A wick through a candle does not count. A close equal to its high or low does not count. The signal candle is not part of the set it closes through.

- Bearish bias and a bearish close-through set direction to Bearish. No sweep is required.
- Bullish bias and a bullish close-through set direction to Bullish.
- Bearish bias from a close below the prior low, and a bullish close-through, set direction to Bullish and flip the bias only after the current HTF candle has swept the previous HTF candle's low.
- Bullish bias from a close above the prior high, and a bearish close-through, set direction to Bearish and flip the bias only after the current HTF candle has swept the previous HTF candle's high.
- A bearish bias from a high sweep that closed back inside ignores a bullish order block until a bearish order block has drawn SELL. After that SELL, a sweep of the previous low and a bullish order block flip the bias and draw BUY. A bullish bias from a low sweep that closed back inside ignores a bearish order block until a bullish order block has drawn BUY. After that BUY, a sweep of the previous high and a bearish order block flip the bias and draw SELL.
- A closed HTF candle that swept both sides of the candle before it is bullish only when its close is above that candle, and bearish only when its close is below that candle. The matching side waits for its own order block before the other side can flip it. After that mark, a sweep of the previous low or high and the opposing order block flip the bias and draw the new label. A close that does not finish above or below the previous candle, including a close on its high or its low and a close above or below its open, leaves the bias None until the first order block of either side. That order block sets the bias, the locked bias, and the direction together, and it draws BUY or SELL. It is not labeled a flip.
- The previous candle for that sweep is the HTF candle that just closed. At 10:00 that is the 9:00 candle.
- The sweep is strict and stays on for the rest of that HTF candle. A confirmed MTF or LTF candle that opened in the current HTF period can mark it. The same MTF candle may sweep with its wick and close through with its close.
- An opposing close-through does nothing until that sweep has happened. Once direction is already set, that candle can still be stored later as the execution array.
- The signal candle must open inside the current HTF period. The candles it closes through may have opened in the previous period.
- While the bias is None for any other reason, a close-through does nothing.
- A close-through that repeats the current direction leaves the sequence in place.
- The candle that sets or flips direction does not store an array. Array search starts on a later MTF candle. The stored array's anchor must still open inside the current HTF period.
- The MTF candle that first sets a bullish direction draws BUY under its low. The candle that first sets a bearish direction draws SELL over its high. A later candle that repeats the same direction does not draw another label. The label stays when the next HTF candle opens.
- **Signal Window Start** and **Signal Window End** are minutes of the exchange hour. A label prints only when the candle's minute is inside that range, including both ends. Start 10 and end 50 print from minute 10 through minute 50. The defaults are 0 and 59, so every minute prints until those inputs change. The dashboard still updates outside the window. Start must be less than or equal to end.
- The stop is fixed when the BUY or SELL direction signal prints. A sell uses the latest LTF swing high. A buy uses the latest LTF swing low. The dashboard **Stop loss** row shows that price. **Stop Line Color** and **Stop Line Type** in Display set the line. The defaults are red and solid. The choices are solid, dashed, and dotted. The line is two LTF candles long on that swing. The BUY or SELL tooltip still shows the price. A later swing does not move it. The next direction signal replaces it. The next HTF open clears the row. If that opposite swing does not exist, the row shows — and nothing is drawn.
- **Timeframe Target** chooses the swing used as the suggested take profit. The default is 15M. Off leaves it empty. A bullish signal uses the newest confirmed swing high of that timeframe that is above the signal candle's high. A bearish signal uses the newest confirmed swing low that is below the signal candle's low. A swing on the signal candle, or on the wrong side of it, is skipped. The pivot uses **LTF MSS Swing Length**. The dashboard **Take profit** row shows the price. **Take Profit Line Color** and **Take Profit Line Type** in Display set the line. The defaults are green and solid. The choices are solid, dashed, and dotted. The line is two candles of the target timeframe, drawn on that swing. The BUY or SELL tooltip also shows the price. A later swing does not move it. The next direction signal replaces it. The next HTF open clears the row. If no swing sits beyond the signal, the row shows — and nothing is drawn.
- An LTF close does not set or flip the bias or the direction. It can mark the sweep.
- A fair value gap does not set or flip the direction.

A new HTF candle reads the bias again, clears the direction, and clears the working array. The MTF candle history used for the close-through stays, and the LTF and target-timeframe swing buffers stay. With a bias and no direction yet, the sequence returns to **BIAS**. With no bias, it returns to **WAITING**.

A bullish direction looks for a bearish array. A bearish direction looks for a bullish array. The array must be created by a candle that opened inside the current HTF period. One array is active at a time.

- **Order block:** the opposing candle, followed by one candle that closes beyond that candle's high (bullish direction) or low (bearish direction) and has a body in the direction. The reference is the opposing candle's open. Its high and low are the zone. If an order block and a fair value gap confirm on the same MTF close, the order block is kept.
- **Fair value gap:** a bearish gap is `candle 1 low > candle 3 high`. A bullish gap is `candle 1 high < candle 3 low`. The reference is candle 3's open. The gap is the zone.

The creating candle is not an inversion candle. Each later completed MTF candle adds one to the countdown.

- Bullish direction: inversion is an MTF close above the stored open.
- Bearish direction: inversion is an MTF close below the stored open.

A wick through the open does not invert it. A close equal to the open does not invert it. The stored open is not replaced by later candles.

## Expiration and reset

`Maximum Candles to Wait for Inversion` is the number of completed MTF candles after identification. The last allowed candle can still invert. If it does not, the array is marked expired, its price and zone are discarded, and the sequence returns to **ARRAY** while the direction remains. An expired array cannot retest or confirm.

A new HTF candle clears the working array and the direction, then reads the bias from the two closed candles. MTF candle history, LTF swings, and target-timeframe swings stay. A setup that reached **CONFIRMED** on the last LTF candle of the old period keeps its chart label. The dashboard then shows the new period.

## MSS/BOS

`LTF MSS Swing Length` is the number of candles required on each side of an LTF pivot. It is not used for direction. The pivot is usable only after those later candles have closed. After the retest, a bullish direction needs a later LTF close above the latest confirmed LTF swing high. A bearish direction needs a later LTF close below the latest confirmed LTF swing low. The retest candle itself cannot be the break. When the direction signal prints, a sell stop is the latest LTF swing high and a buy stop is the latest LTF swing low. The chart marks that price with the Display stop line on that swing, two LTF candles long.

## Non-repaint

Closed MTF candles are read with `time[1]`, `open[1]`, `high[1]`, `low[1]`, and `close[1]` under `lookahead_on`. Closed LTF candles use `time[1]`, `high[1]`, `low[1]`, and `close[1]`. The script acts on those values only after the chart bar that reveals them is confirmed. The HTF period open uses the higher-timeframe `time` because that timestamp does not change during the candle. Bias uses `high[1]`, `low[1]`, and `close[1]` against `high[2]` and `low[2]`. The forming HTF high and low are not read. When the chart is finer than the LTF, the marker is placed on the confirming LTF candle and can appear after that candle has closed.

## Tests

```bash
python3 -m unittest tests.test_obverse -q
```

This workspace has no Pine compiler. Paste the script into TradingView once, with the cursor in column 1, and confirm it compiles on a 1-minute chart before relying on the dashboard. If line 2 or the first input is indented, select all and press Shift+Tab once. TradingView keeps the timeframe choices already saved on the chart.
