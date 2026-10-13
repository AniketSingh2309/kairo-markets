// Kai Academy course content: modules -> lessons, each with a short read, an optional candle illustration,
// a "try it live" action in the Terminal, and a quiz. Kept as data so lessons can be added without code.
// `art`: candles as [open, high, low, close] drawn by academy.js. `try`: { symbol, interval, ind, replay }.
// `quiz`: [{ q, a: [choices], ok: index of the right one, why }].

export const MODULES = [
  {
    id: "basics", title: "How the Indian market works", blurb: "Exchanges, orders, intraday vs delivery, and what a trade really costs.",
    lessons: [
      {
        id: "exchanges", title: "NSE, BSE, SEBI and your demat account",
        body: [
          "Shares of listed Indian companies trade on two exchanges: the National Stock Exchange (NSE) and BSE. SEBI, the market regulator, sets the rules for exchanges, brokers and listed companies.",
          "When you buy shares for delivery they land in your demat account, held with a depository (NSDL or CDSL) through your broker. Trades settle on T+1: the day after you trade, money and shares actually change hands.",
          "Normal trading runs 9:15 am to 3:30 pm IST on weekdays, with a pre-open auction from 9:00 to 9:15 that sets the opening price. The market is shut on exchange holidays.",
        ],
        quiz: [
          { q: "You buy shares for delivery on Monday. When do they settle into your demat account?", a: ["The same day", "Tuesday (T+1)", "Wednesday (T+2)", "At the end of the week"], ok: 1, why: "Indian equities settle on T+1: one trading day after the trade." },
          { q: "Who regulates India's stock exchanges and brokers?", a: ["RBI", "SEBI", "NSDL", "The exchanges regulate themselves"], ok: 1, why: "SEBI (Securities and Exchange Board of India) is the market regulator." },
        ],
      },
      {
        id: "orders", title: "Order types: market, limit, stop-loss, GTT and super orders",
        body: [
          "A market order buys or sells right away at the best available price. Fast, but in a thin stock you can get a worse price than you saw (slippage).",
          "A limit order sets the worst price you'll accept: a buy limit at ₹500 fills at ₹500 or lower. It may never fill. A stop-loss (SL) order waits for a trigger price, then becomes a limit order; SL-M becomes a market order instead.",
          "GTT (good till triggered) waits up to a year for its trigger. A super (bracket) order bundles an entry with a target and a stop-loss: when the entry fills, both exits go live and whichever hits first cancels the other. In Kairo you can drag them on the chart.",
        ],
        try: { label: "Place a practice super order", symbol: "RELIANCE.NS", interval: "5m" },
        quiz: [
          { q: "You want to buy only if the price dips to ₹480. Which order?", a: ["Market order", "Buy limit at ₹480", "Buy SL-M with trigger ₹480", "Sell limit at ₹480"], ok: 1, why: "A buy limit fills at your price or better, so it waits for the dip." },
          { q: "What does a super order add to a normal entry?", a: ["Leverage", "A target and a stop-loss that go live when the entry fills", "Lower charges", "A guaranteed fill"], ok: 1, why: "It attaches both exits at once; whichever triggers first cancels the other." },
          { q: "Your SL-M sell has a trigger at ₹95. Price falls to ₹94.50. What happens?", a: ["Nothing until it closes below ₹95", "It becomes a market sell and exits", "It becomes a limit sell at ₹95 that may not fill", "It is cancelled"], ok: 1, why: "SL-M turns into a market order once the trigger is hit, so it exits even in a fast fall." },
        ],
      },
      {
        id: "intraday", title: "Intraday vs delivery",
        body: [
          "Delivery (CNC) means you pay the full value and hold the shares as long as you like. Intraday (MIS) trades must be closed the same day; brokers lend you margin, typically up to 5× (20% of the value), and square off anything still open near 3:20 pm.",
          "Leverage cuts both ways: with 5× margin a 2% move against you is a 10% loss of the money you put up. In the Indian cash market you can only short-sell intraday; you can't carry a short overnight.",
        ],
        quiz: [
          { q: "With 20% intraday margin you buy ₹1,00,000 of stock using ₹20,000. The price falls 3%. Your loss as a share of your ₹20,000?", a: ["3%", "6%", "15%", "20%"], ok: 2, why: "₹3,000 lost on ₹20,000 put up is 15%. Leverage multiplies the move by 5." },
          { q: "Can you hold a short position in a cash-market stock overnight?", a: ["Yes, as delivery", "No: cash-market shorts are intraday only", "Only on Fridays", "Only with a GTT"], ok: 1, why: "Overnight shorts need the F&O market (futures or options), not cash." },
        ],
      },
      {
        id: "costs", title: "What a trade really costs",
        body: [
          "Every trade pays more than brokerage: Securities Transaction Tax (STT), exchange transaction charges, SEBI's fee, GST on the brokerage and charges, and stamp duty on buys. Selling delivery shares also costs a depository (DP) charge.",
          "These look tiny, but they add up when you trade often. A strategy that makes 0.2% a trade before costs can lose money after them. Intraday scalping is where costs hurt most.",
          "Profits are taxed too: short-term gains on listed shares (held 12 months or less) at 20%, long-term at 12.5% above ₹1.25 lakh a year (rates since 23 July 2024). Intraday profits are business income, taxed at your slab rate. Kairo's Tax P&L estimates this for you.",
        ],
        quiz: [
          { q: "Why do costs matter more for frequent traders?", a: ["They don't", "Costs apply to every trade, so they eat a bigger share of small, frequent gains", "Brokers charge frequent traders double", "STT only applies to frequent traders"], ok: 1, why: "Fixed and percentage costs on every trade compound against small edges." },
          { q: "You sell shares held for 8 months at a profit. That gain is taxed as…", a: ["Long-term capital gain", "Short-term capital gain", "Tax-free", "Business income"], ok: 1, why: "Listed shares held 12 months or less give short-term gains." },
        ],
      },
    ],
  },
  {
    id: "charts", title: "Reading a chart", blurb: "Candles, timeframes, volume and trend structure.",
    lessons: [
      {
        id: "candles", title: "Candlesticks", art: [[100, 108, 97, 106], [106, 107, 98, 99], [99, 103, 95, 101]],
        body: [
          "Each candle covers one time slot. The thick body runs from the open to the close: green (or hollow) if it closed higher than it opened, red if lower. The thin wicks mark the highest and lowest prices traded in that slot.",
          "Long wicks are rejection: price went there and was pushed back. A long lower wick means sellers pushed down but buyers fought back. Big bodies with small wicks show one side in control from start to finish.",
        ],
        try: { label: "Read candles on a live chart", symbol: "^NSEI", interval: "5m" },
        quiz: [
          { q: "A red candle with a long lower wick tells you…", a: ["Buyers controlled the whole period", "Price fell during the period, but buyers pushed it back up from the low", "Volume was high", "The market is closed"], ok: 1, why: "The lower wick is the part of the fall that was rejected." },
          { q: "What do the top and bottom of the wicks show?", a: ["Open and close", "The period's high and low", "Moving averages", "Support and resistance"], ok: 1, why: "Wicks stretch to the highest and lowest traded price." },
        ],
      },
      {
        id: "timeframes", title: "Timeframes: zoom out, then in",
        body: [
          "The same stock tells different stories on 1-minute, 15-minute and daily candles. Short timeframes are noisy: random wiggles look like patterns. Longer ones show the real trend and the levels that matter.",
          "A practical routine: check the daily chart for the trend and key levels, then drop to 15m or 5m to time an entry. Trade in the direction of the higher timeframe.",
        ],
        quiz: [
          { q: "Why are 1-minute candles risky for beginners?", a: ["They're delayed", "Noise: many patterns appear and fail", "They hide volume", "They're only for options"], ok: 1, why: "Short timeframes are full of random moves that look like signals." },
          { q: "The daily chart is in a clear downtrend. A 5-minute chart shows a small bounce. A cautious trader…", a: ["Buys the bounce with full size", "Treats it as a bounce in a downtrend, not a new uptrend", "Ignores the daily chart", "Sells everything"], ok: 1, why: "The higher timeframe sets the context; bounces in downtrends often fail." },
        ],
      },
      {
        id: "volume", title: "Volume: how much conviction",
        body: [
          "Volume is how many shares traded in each candle. A breakout on heavy volume has many participants behind it; the same breakout on thin volume fails more often.",
          "Watch for spikes: 3× the normal volume usually means big players are active, often around news. In India, also look at delivery %: the share of volume actually taken into demat accounts rather than squared off the same day.",
        ],
        try: { label: "Spot volume spikes", symbol: "TCS.NS", interval: "15m", ind: "vol" },
        quiz: [
          { q: "A stock breaks above resistance on half its usual volume. That breakout is…", a: ["Very reliable", "More likely to fail", "Guaranteed to reverse", "Irrelevant to volume"], ok: 1, why: "Low participation means fewer buyers to sustain the move." },
          { q: "High delivery % on an up day suggests…", a: ["Mostly intraday traders", "Buyers who intend to hold", "The stock is banned", "Low liquidity"], ok: 1, why: "Delivered shares were bought to keep, not flipped intraday." },
        ],
      },
      {
        id: "trend", title: "Trend structure: highs and lows",
        body: [
          "An uptrend makes higher highs and higher lows: each pullback stops above the last one. A downtrend makes lower highs and lower lows. Anything else is a range.",
          "Most beginner losses come from fighting the trend, like buying a falling stock because it looks cheap. A simple filter: is price above a rising 50-period average? Then favour buys.",
          "A trend ends when its structure breaks: an uptrend that makes a lower low has lost its shape, even before any indicator says so.",
        ],
        try: { label: "Read the structure with Kai", symbol: "^NSEI", interval: "1h", ind: "sma50" },
        quiz: [
          { q: "Price makes a higher high, then pulls back to a higher low. That's…", a: ["A downtrend", "An uptrend", "A range", "A gap"], ok: 1, why: "Higher highs and higher lows define an uptrend." },
          { q: "An uptrend's latest pullback falls below the previous swing low. This means…", a: ["The uptrend is stronger", "The uptrend structure is broken", "Nothing", "Buy more"], ok: 1, why: "A lower low breaks the pattern of higher lows." },
        ],
      },
    ],
  },
  {
    id: "levels", title: "Support, resistance and key levels", blurb: "Where prices tend to stall, and why.",
    lessons: [
      {
        id: "sr", title: "Support and resistance",
        body: [
          "Support is a price where buying has stopped falls before; resistance is where selling has stopped rises. They form because traders remember: people who missed buying at ₹500 last time buy there again.",
          "Levels are zones, not exact lines. The more times a level is tested, the more it matters; but each test also uses up orders sitting there, so a fourth test often breaks through.",
          "When resistance breaks it often becomes support (and vice versa): the old ceiling becomes the new floor.",
        ],
        try: { label: "See Kai's levels on a chart", symbol: "INFY.NS", interval: "15m" },
        quiz: [
          { q: "A stock broke above resistance at ₹1,000 and then pulls back to ₹1,005. Many traders expect…", a: ["₹1,000 to act as support now", "A crash", "Resistance at ₹1,005", "Nothing"], ok: 0, why: "Broken resistance often flips to support on the retest." },
          { q: "Support and resistance are best treated as…", a: ["Exact prices", "Zones", "Indicators", "Random"], ok: 1, why: "Orders cluster around a level, not at one tick." },
        ],
      },
      {
        id: "daylevels", title: "Intraday levels: previous day, opening range, VWAP",
        body: [
          "Intraday traders watch the previous day's high, low and close: they're where yesterday's buyers and sellers gave up. Breaking yesterday's high early in the day is a sign of strength.",
          "The opening range is the high and low of the first 15 minutes. A break of it is a popular setup (opening-range breakout), best when the index agrees.",
          "VWAP is the volume-weighted average price for the day: the average price everyone has paid. Above VWAP, buyers are in control; institutions often judge their fills against it.",
        ],
        try: { label: "Watch VWAP live", symbol: "HDFCBANK.NS", interval: "5m", ind: "vwap" },
        quiz: [
          { q: "What is VWAP?", a: ["Yesterday's close", "The day's average traded price, weighted by volume", "A 20-day moving average", "The opening price"], ok: 1, why: "VWAP weights each price by the volume traded at it, resetting daily." },
          { q: "The opening range is…", a: ["The previous day's range", "The high and low of the first 15 minutes", "The pre-open auction price", "The 52-week range"], ok: 1, why: "It's the range set in the first minutes of the session." },
        ],
      },
      {
        id: "breakouts", title: "Breakouts and false breakouts",
        body: [
          "A breakout is price leaving a range or crossing a level. Real breakouts usually close beyond the level on rising volume and don't fall straight back.",
          "Many breakouts fail: price pokes through, triggers stop-losses and breakout buyers, then reverses. That failed break is itself a signal: trapped buyers sell, fuelling the move the other way.",
          "Practical rule: wait for the candle to close beyond the level, or for a retest that holds. You'll miss a few moves and avoid many traps.",
        ],
        try: { label: "Practise breakouts in Bar Replay", symbol: "SBIN.NS", interval: "15m", replay: true },
        quiz: [
          { q: "Price spikes above resistance mid-candle but closes back below it. That's…", a: ["A confirmed breakout", "A failed breakout (rejection)", "A gap", "A golden cross"], ok: 1, why: "The close back inside the range shows the break was rejected." },
          { q: "Which makes a breakout more trustworthy?", a: ["Low volume", "A close beyond the level on rising volume", "Happening at 3:25 pm", "A doji"], ok: 1, why: "Closes and volume show real commitment." },
        ],
      },
    ],
  },
  {
    id: "patterns", title: "Candlestick and chart patterns", blurb: "The patterns Kai spots, what they mean, and when to ignore them.",
    lessons: [
      {
        id: "single", title: "One-candle patterns: doji, hammer, shooting star, marubozu",
        art: [[100, 102, 98, 100.2], [103, 103.6, 97, 103.2], [100, 106, 99.6, 100.5], [100, 107.2, 99.9, 107]],
        body: [
          "Doji (first): open and close almost equal. Indecision. After a long move it can mean the move is tiring.",
          "Hammer (second): long lower wick, small body at the top, after a fall. Sellers pushed down, buyers drove it back. The same shape after a rise is a hanging man, a weaker warning.",
          "Shooting star (third): long upper wick after a rise, buyers rejected. Marubozu (fourth): a big body with almost no wicks; one side controlled the whole period.",
          "A single candle is a hint, not a signal. Wait for the next candle to confirm, and weight it more when it forms at a support or resistance level.",
        ],
        quiz: [
          { q: "A hammer is most meaningful…", a: ["In the middle of a range", "After a fall, at a support level", "After a long rise", "On low volume"], ok: 1, why: "Rejection of lower prices matters most where buyers are expected." },
          { q: "What confirms a hammer?", a: ["Nothing; it's enough alone", "The next candle closing above the hammer's high", "A red next candle", "The RSI being 50"], ok: 1, why: "Follow-through shows buyers really took over." },
        ],
      },
      {
        id: "multi", title: "Two- and three-candle patterns",
        art: [[105, 106, 99, 100], [99, 107, 98.5, 106.5], [106, 106.5, 103, 103.6], [103.5, 104, 102.6, 103.2], [103, 108, 102.8, 107.6]],
        body: [
          "Bullish engulfing (first two candles): a green body that swallows the previous red body. Buyers overwhelmed sellers. Bearish engulfing is the mirror.",
          "Morning star (last three): a big red candle, a small pause candle, then a strong green one closing above the first candle's midpoint. Selling ran out. The evening star is its bearish twin.",
          "Harami: a small candle inside the previous big one, the move is slowing. Tweezer bottom/top: two candles with the same low/high, a level defended twice. Three white soldiers / three black crows: three strong candles in a row.",
        ],
        try: { label: "Let Kai spot patterns live", symbol: "^NSEBANK", interval: "15m" },
        quiz: [
          { q: "In a bullish engulfing pattern, the green body…", a: ["Is smaller than the red one", "Completely covers the previous red body", "Has no wicks", "Opens higher"], ok: 1, why: "Engulfing means the second body covers the first." },
          { q: "A morning star needs…", a: ["Three green candles", "A big red candle, a small candle, then a strong green one", "Two dojis", "A gap down"], ok: 1, why: "It shows selling exhausting and buyers taking over." },
        ],
      },
      {
        id: "chartpat", title: "Chart patterns: double tops and bottoms, triangles, flags",
        body: [
          "Double top: two peaks at about the same price with a valley between. It's only confirmed when price closes below the valley (the neckline). The textbook target is the pattern's height projected down. Double bottoms are the mirror.",
          "Triangles: lower highs and higher lows squeeze price into a point; pressure builds until it breaks out. Flags: a sharp move, then a small orderly pullback; the move often resumes.",
          "Patterns are probabilities, not promises. Trade the confirmation (the neckline break), place the stop where the pattern is proven wrong, and expect some to fail.",
        ],
        quiz: [
          { q: "When is a double top confirmed?", a: ["At the second peak", "When price closes below the neckline", "When RSI is above 70", "Immediately after the first peak"], ok: 1, why: "Until the neckline breaks, it's just two highs." },
          { q: "A narrowing triangle means…", a: ["Volatility is expanding", "Pressure is building; a breakout is likely, direction unknown", "The trend is guaranteed to continue", "Volume must be high"], ok: 1, why: "Squeezing ranges tend to resolve with a sharp move." },
        ],
      },
    ],
  },
  {
    id: "indicators", title: "Indicators", blurb: "Moving averages, RSI, MACD, Bollinger Bands, and why they lag.",
    lessons: [
      {
        id: "ma", title: "Moving averages",
        body: [
          "A moving average smooths price: the 20-period average is the mean of the last 20 closes. The EMA weights recent prices more, so it reacts faster.",
          "Uses: trend filter (price above a rising 50 or 200 average), dynamic support in trends (pullbacks to the 20 EMA), and crossovers. The golden cross (50 above 200) and death cross (50 below 200) are slow, widely watched signals.",
          "Averages lag by design: they tell you what has happened. In sideways markets, crossovers whipsaw back and forth.",
        ],
        try: { label: "Add moving averages", symbol: "RELIANCE.NS", interval: "1D", ind: "sma50" },
        quiz: [
          { q: "Why do crossover signals fail in sideways markets?", a: ["Averages are wrong", "Price crosses the averages back and forth, creating whipsaws", "Volume is low", "They work best then"], ok: 1, why: "Without a trend, averages flatten and price keeps crossing them." },
          { q: "A golden cross is…", a: ["The 50 average crossing above the 200", "RSI above 70", "Price above VWAP", "A candlestick"], ok: 0, why: "It's the 50-period average moving above the 200-period one." },
        ],
      },
      {
        id: "rsi", title: "RSI and divergence",
        body: [
          "RSI (14) compares recent up moves with down moves on a 0 to 100 scale. Above 70 is called overbought, below 30 oversold.",
          "Overbought doesn't mean sell: in strong uptrends RSI can stay above 70 for weeks. Use it as 'stretched, don't chase', not as a reversal signal.",
          "Divergence is more useful: price makes a higher high but RSI makes a lower one. The rally is losing strength. It warns; it doesn't time the turn.",
        ],
        try: { label: "Watch RSI live", symbol: "^NSEI", interval: "15m", ind: "rsi" },
        quiz: [
          { q: "RSI is 78 in a strong uptrend. The sensible reading is…", a: ["Sell immediately", "The move is stretched: don't chase, but it can stay high", "The stock is about to crash", "RSI is broken"], ok: 1, why: "Overbought can persist in trends." },
          { q: "Bearish divergence is…", a: ["Price higher high, RSI lower high", "Price lower low, RSI lower low", "RSI above 70", "MACD below zero"], ok: 0, why: "Momentum fading while price still rises." },
        ],
      },
      {
        id: "macdbb", title: "MACD and Bollinger Bands",
        body: [
          "MACD tracks the gap between a fast (12) and slow (26) EMA; its signal line is a 9-period EMA of that gap. MACD crossing above the signal means momentum is turning up. It's lagging and works best with the trend.",
          "Bollinger Bands sit two standard deviations above and below a 20-period average. Price outside the bands is unusual. Narrow bands (a squeeze) mean volatility has dried up; a big move often follows, direction unknown.",
        ],
        try: { label: "Find a Bollinger squeeze", symbol: "ICICIBANK.NS", interval: "15m", ind: "bb" },
        quiz: [
          { q: "A Bollinger squeeze tells you…", a: ["The direction of the next move", "Volatility is low; a large move often follows", "To sell", "Volume is high"], ok: 1, why: "The squeeze is about volatility, not direction." },
          { q: "MACD signals are strongest…", a: ["In sideways markets", "In the direction of the bigger trend", "At 9:15 am", "On 1-minute charts"], ok: 1, why: "Against the trend, MACD crosses often fail." },
        ],
      },
    ],
  },
  {
    id: "risk", title: "Risk and psychology", blurb: "The part that decides whether you survive: sizing, stops, expectancy, discipline.",
    lessons: [
      {
        id: "sizing", title: "Position sizing: the 1% rule",
        body: [
          "Decide the stop first, then the size. If your stop is ₹10 below your entry and you're willing to lose 1% of a ₹5,00,000 account (₹5,000), you can buy 500 shares, not more.",
          "Why 1 to 2%: a run of ten losers, which happens to every trader, costs about 10 to 20%, which you can recover from. Risking 10% a trade, the same run wipes you out.",
          "Kai's plans and the super-order ticket do this arithmetic for you.",
        ],
        quiz: [
          { q: "Account ₹2,00,000, risk 1%, entry ₹250, stop ₹245. Maximum shares?", a: ["40", "400", "800", "4,000"], ok: 1, why: "1% is ₹2,000; risk per share is ₹5; ₹2,000 ÷ ₹5 = 400." },
          { q: "What do you decide first?", a: ["Quantity", "Where the stop goes", "The target only", "The broker"], ok: 1, why: "The stop sets the risk per share, which sets the size." },
        ],
      },
      {
        id: "stops", title: "Stops, targets and reward to risk",
        body: [
          "Put the stop where your idea is proven wrong (just beyond the support you're buying at), not at a round amount you're comfortable losing.",
          "Reward to risk compares the distance to your target with the distance to your stop. At 2 : 1 you can be right only 40% of the time and still make money. At 0.5 : 1 you need to be right two times in three.",
          "Never move a stop further away to avoid being stopped out. Moving it closer to lock in profit (trailing) is fine.",
        ],
        try: { label: "Set a trailing super order", symbol: "INFY.NS", interval: "15m" },
        quiz: [
          { q: "Win rate 40%, average win ₹2,000, average loss ₹1,000. Over 10 trades you…", a: ["Lose ₹2,000", "Make ₹2,000", "Break even", "Make ₹8,000"], ok: 1, why: "4 × ₹2,000 − 6 × ₹1,000 = +₹2,000." },
          { q: "Your stop is about to be hit. Moving it further away is…", a: ["Smart risk management", "Turning a planned loss into an unplanned bigger one", "Required by SEBI", "Called trailing"], ok: 1, why: "Trailing moves stops toward profit, never away." },
        ],
      },
      {
        id: "psych", title: "Discipline: overtrading, revenge trading, journaling",
        body: [
          "Overtrading: taking trades because you're bored or want action. Costs pile up and quality falls. No setup is a valid decision.",
          "Revenge trading: doubling size after a loss to win it back. It's how small losses become account-ending ones. Set a daily loss limit and stop when you hit it.",
          "Keep a journal: entry reason, stop, exit, and how you felt. After 20 trades you'll see which setups work for you and which mistakes repeat. Kairo's trade journal does the numbers.",
        ],
        quiz: [
          { q: "After three losses you want to double your size to recover. This is…", a: ["Good money management", "Revenge trading", "Position sizing", "A trailing stop"], ok: 1, why: "Raising size to chase losses is the classic path to large drawdowns." },
          { q: "A trading journal mainly helps you…", a: ["Pay less tax", "Find which setups and habits actually work for you", "Get better prices", "Avoid STT"], ok: 1, why: "Your own data shows your edge and your repeated mistakes." },
        ],
      },
    ],
  },
  {
    id: "fno", title: "Futures and options basics", blurb: "Lots, premiums, expiry, the Greeks, and the real odds for retail traders.",
    lessons: [
      {
        id: "futures", title: "Futures",
        body: [
          "A futures contract is an agreement to buy or sell an index or stock at a set price on an expiry date. It trades in fixed lot sizes set by NSE, and you only put up margin (a fraction of the value), so it's leveraged.",
          "Gains and losses are settled daily (mark-to-market). Futures let you go short overnight, which the cash market doesn't allow.",
        ],
        quiz: [
          { q: "Why are futures risky for beginners?", a: ["They have no expiry", "They're leveraged: a small move is a large share of your margin", "They can't be shorted", "They're settled in gold"], ok: 1, why: "Margin is a fraction of the contract value." },
          { q: "Futures trade in…", a: ["Any quantity", "Lots of a fixed size set by the exchange", "Single shares", "Rupee amounts"], ok: 1, why: "Each contract has an exchange-set lot size." },
        ],
      },
      {
        id: "options", title: "Calls, puts, premium and expiry",
        body: [
          "A call option gives the buyer the right to buy at the strike price until expiry; a put gives the right to sell. The buyer pays a premium; their maximum loss is that premium.",
          "Option sellers (writers) collect the premium but take on the obligation; their losses can be large, so they must post margin.",
          "Premium = intrinsic value + time value. Time value decays toward zero by expiry, fastest in the last days. That decay works against buyers every day the price doesn't move.",
          "SEBI's studies of retail F&O traders found that roughly nine in ten individuals lost money. Learn on paper first; Kairo's strategy builder shows the payoff before you trade.",
        ],
        try: { label: "Open the option chain", symbol: "^NSEI", interval: "5m", chain: true },
        quiz: [
          { q: "You buy a call for ₹50 premium. Your maximum loss is…", a: ["Unlimited", "₹50 per share (the premium)", "The strike price", "Zero"], ok: 1, why: "An option buyer can lose at most the premium paid." },
          { q: "Time decay (theta) hurts…", a: ["Option buyers", "Option sellers", "Futures traders only", "Nobody"], ok: 0, why: "Buyers' premium loses time value each day." },
        ],
      },
      {
        id: "greeks", title: "The Greeks in plain words",
        body: [
          "Delta: how much the option price moves for a ₹1 move in the underlying. An at-the-money call has a delta near 0.5.",
          "Theta: how much value the option loses per day from time decay. Vega: sensitivity to implied volatility (IV). Options get expensive when IV is high, often before results or big events, and cheap after.",
          "A common beginner trap: buying options before results when IV is high, being right on direction, and still losing because IV collapses afterwards (IV crush).",
        ],
        quiz: [
          { q: "An option with delta 0.5. The stock rises ₹10. The option rises about…", a: ["₹10", "₹5", "₹0.50", "₹50"], ok: 1, why: "Delta 0.5 means about half the underlying's move." },
          { q: "IV crush is…", a: ["A sharp drop in implied volatility, often right after an event", "A type of order", "A margin call", "A circuit breaker"], ok: 0, why: "Once the uncertainty passes, IV and premiums fall." },
        ],
      },
    ],
  },
  {
    id: "india", title: "Reading India's market data", blurb: "FII/DII flows, delivery, deals, surveillance and results season.",
    lessons: [
      {
        id: "flows", title: "FII / DII flows and positioning",
        body: [
          "FIIs (foreign institutional investors) and DIIs (domestic institutions such as mutual funds and insurers) publish net buying and selling every day. Heavy FII selling often pressures large caps; strong DII buying (fed by SIP flows) often cushions falls.",
          "Participant-wise open interest shows who is long or short index futures. When FIIs are, say, 85 to 90% short, that's a crowded bet: short covering can spark sharp rallies.",
        ],
        try: { label: "Open Pulse", page: "pulse" },
        quiz: [
          { q: "DIIs include…", a: ["Foreign hedge funds", "Indian mutual funds and insurers", "Retail traders", "SEBI"], ok: 1, why: "Domestic institutional investors are Indian institutions." },
          { q: "FIIs are 90% short index futures. A risk for those shorts is…", a: ["None", "A short-covering rally", "Lower margins", "IV crush"], ok: 1, why: "Crowded shorts buying back at once can drive prices up fast." },
        ],
      },
      {
        id: "surv", title: "Surveillance, circuits and the F&O ban",
        body: [
          "Exchanges put stocks with unusual moves or weak fundamentals under ASM (additional surveillance) or GSM (graded surveillance). Higher margins, trade-for-trade settlement or limited trading days can apply. Beginners should usually avoid them.",
          "Stocks also have price bands (circuits), typically 2%, 5%, 10% or 20% a day; at the limit, trading can freeze on one side and you may not be able to exit.",
          "When open interest in a stock's F&O crosses 95% of the market-wide limit, it enters the F&O ban: no new positions until it falls back below 80%.",
        ],
        quiz: [
          { q: "A stock is stuck at its lower circuit. Why is that dangerous for a holder?", a: ["It isn't", "There may be no buyers, so you can't exit", "Brokerage doubles", "It becomes an F&O stock"], ok: 1, why: "At the lower band, sellers queue with few or no buyers." },
          { q: "In the F&O ban period you can…", a: ["Open new F&O positions freely", "Only reduce existing F&O positions", "Not trade the stock at all in cash", "Only buy options"], ok: 1, why: "The ban stops fresh positions; existing ones can be closed." },
        ],
      },
      {
        id: "results", title: "Results season, dividends and corporate actions",
        body: [
          "Quarterly results move stocks the most. Spreads widen and gaps are common; many traders reduce size or stay flat into results. Kairo's calendar shows your stocks' result dates.",
          "On the ex-dividend date, the price drops by about the dividend: you need to own the shares before the ex-date to get it. Splits and bonus issues change the share count and price, not the value of your holding.",
          "Bulk and block deals show large investors buying or selling. A well-known fund buying in size is information, not a recommendation.",
        ],
        try: { label: "See the calendar", page: "pulse/calendar" },
        quiz: [
          { q: "To receive a dividend you must own the shares…", a: ["On the payment date", "Before the ex-dividend date", "Within a month after", "At any time that year"], ok: 1, why: "Buyers on or after the ex-date don't get that dividend." },
          { q: "A 1:1 bonus issue. Your 100 shares at ₹400 become…", a: ["100 shares at ₹800", "200 shares at about ₹200", "200 shares at ₹400", "50 shares at ₹800"], ok: 1, why: "The share count doubles and the price roughly halves; value is unchanged." },
        ],
      },
    ],
  },
];

export const LESSON_COUNT = MODULES.reduce((a, m) => a + m.lessons.length, 0);
