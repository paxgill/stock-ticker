/**
 * options_strategies.js — 15 institutional options strategy presets
 * MIT License — https://pg-stock-ticker.up.railway.app
 *
 * Each object contains ALL fields required by the options strategy engine:
 * entry criteria, Greeks targets, risk profile, management rules,
 * position sizing, and questionnaire matching weights.
 */

const OPTIONS_STRATEGIES = [

  // ── 1. Iron Condor ───────────────────────────────────────────────────────
  {
    id: 'iron_condor',
    name: 'Iron Condor',
    category: 'Neutral',
    type: 'Income',
    description: 'Sell an OTM call spread and an OTM put spread simultaneously, collecting a net credit. The trade profits if the underlying stays within the two short strikes at expiration. Ideal for high-IV environments where implied volatility is expected to contract back toward the mean.',
    legs: 4,

    ivr_min: 50, ivr_max: null,
    rsi_signal: 'neutral',
    trend_requirement: 'consolidating',
    iv_regime: 'high',
    entry_signal: 'Enter when IVR > 50 and price is consolidating between Bollinger Bands with RSI between 40–60.',

    delta_target: 'Net 0',
    delta_range: [-0.05, 0.05],
    theta_bias: 'positive',
    vega_bias: 'negative',
    gamma_bias: 'negative',

    risk_type: 'defined',
    max_gain: 'Net credit received',
    max_loss: 'Spread width minus net credit',
    breakeven: 'Short call + credit  /  Short put − credit',

    profit_target_pct: 50,
    stop_loss_rule: '2× initial credit received',
    dte_entry: 45,
    dte_exit: 21,
    rolling_rule: 'Roll untested spread toward ATM if challenged; close if spread doubles in value',

    market_condition: 'Range-bound with contracting volatility (IVR > 50)',
    earnings_play: false,

    capital_pct_max: 5,
    capital_tier: 'medium',
    margin_required: true,

    compatible_views: ['neutral'],
    compatible_objectives: ['income'],
    compatible_risk: ['defined_only', 'defined_wide'],
    compatible_horizons: ['swing'],
    compatible_greeks_pref: ['theta', 'negative_vega'],
  },

  // ── 2. Iron Butterfly ────────────────────────────────────────────────────
  {
    id: 'iron_butterfly',
    name: 'Iron Butterfly',
    category: 'Neutral',
    type: 'Income',
    description: 'Sell an ATM straddle and buy OTM wings for protection, collecting maximum premium when price is pinned at the short strike at expiration. Higher credit than an Iron Condor but requires much tighter price movement — best suited to extremely low-movement, high-IV environments.',
    legs: 4,

    ivr_min: 60, ivr_max: null,
    rsi_signal: 'neutral',
    trend_requirement: 'consolidating',
    iv_regime: 'high',
    entry_signal: 'Enter at IVR > 60 with stock near a key price level and low 5-day ATR indicating tight range.',

    delta_target: 'Net 0',
    delta_range: [-0.05, 0.05],
    theta_bias: 'positive',
    vega_bias: 'negative',
    gamma_bias: 'negative',

    risk_type: 'defined',
    max_gain: 'Net credit received (price exactly at short strike at expiration)',
    max_loss: 'Wing width minus net credit',
    breakeven: 'Short strike ± net credit received',

    profit_target_pct: 25,
    stop_loss_rule: '2× initial credit received',
    dte_entry: 30,
    dte_exit: 14,
    rolling_rule: 'Roll entire position to new ATM if stock moves beyond one wing width',

    market_condition: 'Extremely range-bound, low-movement environment with high implied volatility',
    earnings_play: false,

    capital_pct_max: 5,
    capital_tier: 'medium',
    margin_required: true,

    compatible_views: ['neutral'],
    compatible_objectives: ['income'],
    compatible_risk: ['defined_only', 'defined_wide'],
    compatible_horizons: ['swing'],
    compatible_greeks_pref: ['theta', 'negative_vega'],
  },

  // ── 3. Bull Put Spread ───────────────────────────────────────────────────
  {
    id: 'bull_put_spread',
    name: 'Bull Put Spread',
    category: 'Bullish',
    type: 'Directional',
    description: 'Sell an OTM put and buy a lower-strike put for protection, collecting a net credit while expressing a bullish view. Profits if the stock stays above the short put strike at expiration. A defined-risk, capital-efficient alternative to selling a naked put.',
    legs: 2,

    ivr_min: 30, ivr_max: null,
    rsi_signal: 'oversold',
    trend_requirement: 'above_200sma',
    iv_regime: 'any',
    entry_signal: 'Enter on dips with RSI < 40 when stock is above its 200-day SMA and IVR > 30.',

    delta_target: '0.30',
    delta_range: [0.15, 0.45],
    theta_bias: 'positive',
    vega_bias: 'negative',
    gamma_bias: 'negative',

    risk_type: 'defined',
    max_gain: 'Net credit received',
    max_loss: 'Spread width minus net credit',
    breakeven: 'Short put strike minus net credit',

    profit_target_pct: 50,
    stop_loss_rule: '2× initial credit or if short put goes in-the-money',
    dte_entry: 30,
    dte_exit: 14,
    rolling_rule: 'Roll down and out for credit if stock drops through short strike with time remaining',

    market_condition: 'Moderately bullish with support levels intact and rising or flat trend',
    earnings_play: false,

    capital_pct_max: 5,
    capital_tier: 'low',
    margin_required: false,

    compatible_views: ['bullish'],
    compatible_objectives: ['income', 'growth'],
    compatible_risk: ['defined_only', 'defined_wide'],
    compatible_horizons: ['swing'],
    compatible_greeks_pref: ['theta', 'delta'],
  },

  // ── 4. Bear Call Spread ──────────────────────────────────────────────────
  {
    id: 'bear_call_spread',
    name: 'Bear Call Spread',
    category: 'Bearish',
    type: 'Directional',
    description: 'Sell an OTM call and buy a higher-strike call for protection, collecting a net credit while expressing a bearish view. Profits when the stock stays below the short call strike at expiration. The defined-risk, lower-capital counterpart to selling a naked call.',
    legs: 2,

    ivr_min: 30, ivr_max: null,
    rsi_signal: 'overbought',
    trend_requirement: 'below_200sma',
    iv_regime: 'any',
    entry_signal: 'Enter on rallies with RSI > 60 when stock is below its 200-day SMA and IVR > 30.',

    delta_target: '-0.30',
    delta_range: [-0.45, -0.15],
    theta_bias: 'positive',
    vega_bias: 'negative',
    gamma_bias: 'negative',

    risk_type: 'defined',
    max_gain: 'Net credit received',
    max_loss: 'Spread width minus net credit',
    breakeven: 'Short call strike plus net credit',

    profit_target_pct: 50,
    stop_loss_rule: '2× initial credit or if short call goes in-the-money',
    dte_entry: 30,
    dte_exit: 14,
    rolling_rule: 'Roll up and out for credit if stock rallies through short strike with time remaining',

    market_condition: 'Moderately bearish with resistance levels intact and declining or flat trend',
    earnings_play: false,

    capital_pct_max: 5,
    capital_tier: 'low',
    margin_required: false,

    compatible_views: ['bearish'],
    compatible_objectives: ['income', 'growth'],
    compatible_risk: ['defined_only', 'defined_wide'],
    compatible_horizons: ['swing'],
    compatible_greeks_pref: ['theta', 'delta'],
  },

  // ── 5. Jade Lizard ───────────────────────────────────────────────────────
  {
    id: 'jade_lizard',
    name: 'Jade Lizard',
    category: 'Neutral/Bullish',
    type: 'Income',
    description: 'Sell an OTM put and an OTM call spread simultaneously, collecting a total credit that exceeds the call spread width — eliminating upside risk entirely. A versatile high-credit structure that profits if the stock stays above the short put and below the short call at expiration.',
    legs: 3,

    ivr_min: 50, ivr_max: null,
    rsi_signal: 'neutral',
    trend_requirement: 'any',
    iv_regime: 'high',
    entry_signal: 'Enter when IVR > 50 and total credit exceeds the call spread width, ensuring zero upside risk at any price above the short call.',

    delta_target: '0.15',
    delta_range: [0.05, 0.25],
    theta_bias: 'positive',
    vega_bias: 'negative',
    gamma_bias: 'negative',

    risk_type: 'undefined',
    max_gain: 'Net credit received',
    max_loss: 'Short put strike minus total credit (stock goes to zero)',
    breakeven: 'Short put strike minus net credit',

    profit_target_pct: 50,
    stop_loss_rule: 'Close if short put doubles in value or position delta exceeds −0.40',
    dte_entry: 45,
    dte_exit: 21,
    rolling_rule: 'Roll short put down and out for credit if stock approaches that strike',

    market_condition: 'Range-bound to mildly bullish with elevated implied volatility (IVR > 50)',
    earnings_play: false,

    capital_pct_max: 5,
    capital_tier: 'medium',
    margin_required: true,

    compatible_views: ['neutral', 'bullish'],
    compatible_objectives: ['income'],
    compatible_risk: ['defined_wide', 'undefined'],
    compatible_horizons: ['swing'],
    compatible_greeks_pref: ['theta', 'negative_vega'],
  },

  // ── 6. Broken Wing Butterfly (Call) ─────────────────────────────────────
  {
    id: 'bwb_call',
    name: 'Broken Wing Butterfly (Call)',
    category: 'Neutral/Bearish',
    type: 'Income',
    description: 'A modified call butterfly where the upper wing is widened, creating an asymmetric structure that typically collects a small net credit with zero upside risk. Profits from time decay when the stock stays range-bound to mildly bearish, with the credit making it a zero-cost-or-better trade.',
    legs: 3,

    ivr_min: 30, ivr_max: null,
    rsi_signal: 'neutral',
    trend_requirement: 'consolidating',
    iv_regime: 'any',
    entry_signal: 'Enter for a net credit when stock is near the middle strike and expected to stay range-bound to slightly lower.',

    delta_target: '-0.10',
    delta_range: [-0.20, 0.05],
    theta_bias: 'positive',
    vega_bias: 'neutral',
    gamma_bias: 'negative',

    risk_type: 'defined',
    max_gain: 'Net credit + lower spread width (at middle strike at expiration)',
    max_loss: 'Lower spread width minus upper spread width minus credit',
    breakeven: 'Lower body + net credit  /  Upper body + remaining width',

    profit_target_pct: 50,
    stop_loss_rule: 'Close if loss exceeds 2× credit received',
    dte_entry: 30,
    dte_exit: 14,
    rolling_rule: 'Convert to Iron Condor by adding a put spread if stock drops significantly',

    market_condition: 'Range-bound to mildly bearish with moderate implied volatility',
    earnings_play: false,

    capital_pct_max: 5,
    capital_tier: 'low',
    margin_required: false,

    compatible_views: ['neutral', 'bearish'],
    compatible_objectives: ['income'],
    compatible_risk: ['defined_only', 'defined_wide'],
    compatible_horizons: ['swing'],
    compatible_greeks_pref: ['theta'],
  },

  // ── 7. Broken Wing Butterfly (Put) ──────────────────────────────────────
  {
    id: 'bwb_put',
    name: 'Broken Wing Butterfly (Put)',
    category: 'Neutral/Bullish',
    type: 'Income',
    description: 'A modified put butterfly where the lower wing is widened, creating an asymmetric structure that collects a small credit with zero downside risk. The most popular BWB variant due to natural put-skew advantage — profits from time decay when the stock stays range-bound to mildly bullish.',
    legs: 3,

    ivr_min: 30, ivr_max: null,
    rsi_signal: 'neutral',
    trend_requirement: 'consolidating',
    iv_regime: 'any',
    entry_signal: 'Enter for a net credit when stock is near the middle strike, leveraging put skew for additional premium.',

    delta_target: '0.10',
    delta_range: [-0.05, 0.20],
    theta_bias: 'positive',
    vega_bias: 'neutral',
    gamma_bias: 'negative',

    risk_type: 'defined',
    max_gain: 'Net credit + upper spread width (at middle strike at expiration)',
    max_loss: 'Upper spread width minus lower spread width minus credit',
    breakeven: 'Upper body − net credit  /  Lower body − remaining width',

    profit_target_pct: 50,
    stop_loss_rule: 'Close if loss exceeds 2× credit received',
    dte_entry: 30,
    dte_exit: 14,
    rolling_rule: 'Convert to Iron Condor by adding a call spread if stock rallies significantly',

    market_condition: 'Range-bound to mildly bullish with moderate-to-high put skew',
    earnings_play: false,

    capital_pct_max: 5,
    capital_tier: 'low',
    margin_required: false,

    compatible_views: ['neutral', 'bullish'],
    compatible_objectives: ['income'],
    compatible_risk: ['defined_only', 'defined_wide'],
    compatible_horizons: ['swing'],
    compatible_greeks_pref: ['theta'],
  },

  // ── 8. Put Ratio Spread (1×2) ────────────────────────────────────────────
  {
    id: 'put_ratio_spread',
    name: 'Put Ratio Spread (1×2)',
    category: 'Bearish/Neutral',
    type: 'Directional',
    description: 'Buy one ATM or slightly OTM put and sell two lower-strike puts, often for a net credit or zero cost. Profits if the stock declines moderately to the short strikes; undefined risk below the lower strikes. A cost-efficient bearish structure that benefits from IV contraction at initiation.',
    legs: 3,

    ivr_min: 40, ivr_max: null,
    rsi_signal: 'overbought',
    trend_requirement: 'below_200sma',
    iv_regime: 'high',
    entry_signal: 'Enter for zero cost or small credit when IVR > 40 and stock shows a technical topping pattern.',

    delta_target: '-0.30',
    delta_range: [-0.50, -0.10],
    theta_bias: 'positive',
    vega_bias: 'negative',
    gamma_bias: 'negative',

    risk_type: 'undefined',
    max_gain: 'Distance between long and short puts at expiration',
    max_loss: 'Unlimited below lower short puts (minus credit collected)',
    breakeven: '(2 × lower strike) − long strike ± net premium',

    profit_target_pct: 50,
    stop_loss_rule: 'Close if loss equals the distance between the two strikes',
    dte_entry: 45,
    dte_exit: 21,
    rolling_rule: 'Close naked short puts immediately if stock gaps through both short strikes',

    market_condition: 'Moderately bearish, expecting controlled decline — not a crash scenario',
    earnings_play: false,

    capital_pct_max: 5,
    capital_tier: 'medium',
    margin_required: true,

    compatible_views: ['bearish', 'neutral'],
    compatible_objectives: ['income', 'growth'],
    compatible_risk: ['defined_wide', 'undefined'],
    compatible_horizons: ['swing'],
    compatible_greeks_pref: ['theta', 'delta', 'negative_vega'],
  },

  // ── 9. Calendar Spread ───────────────────────────────────────────────────
  {
    id: 'calendar_spread',
    name: 'Calendar Spread',
    category: 'Neutral',
    type: 'Volatility',
    description: 'Sell a near-term option and buy a longer-term option at the same strike, profiting from the faster time decay of the short leg and potential IV expansion in the long leg. Best entered in low-IV environments at key technical levels, particularly effective around earnings events.',
    legs: 2,

    ivr_min: null, ivr_max: 40,
    rsi_signal: 'neutral',
    trend_requirement: 'consolidating',
    iv_regime: 'low',
    entry_signal: 'Enter when IVR < 40 at a key support or resistance level with front-month IV lower than back-month (normal contango).',

    delta_target: 'Net 0',
    delta_range: [-0.10, 0.10],
    theta_bias: 'positive',
    vega_bias: 'positive',
    gamma_bias: 'negative',

    risk_type: 'defined',
    max_gain: 'Maximum when stock is exactly at short strike at front-month expiration',
    max_loss: 'Net debit paid',
    breakeven: 'Short strike ± width of the profit tent',

    profit_target_pct: 25,
    stop_loss_rule: 'Close if position value drops to 50% of initial debit',
    dte_entry: 45,
    dte_exit: 21,
    rolling_rule: 'Roll front-month short to next expiration when it reaches 21 DTE',

    market_condition: 'Range-bound near a technical level with low IV and potential for volatility expansion',
    earnings_play: true,

    capital_pct_max: 3,
    capital_tier: 'low',
    margin_required: false,

    compatible_views: ['neutral'],
    compatible_objectives: ['income', 'growth'],
    compatible_risk: ['defined_only', 'defined_wide'],
    compatible_horizons: ['swing', 'long_term'],
    compatible_greeks_pref: ['theta', 'vega'],
  },

  // ── 10. Diagonal Spread ──────────────────────────────────────────────────
  {
    id: 'diagonal_spread',
    name: 'Diagonal Spread',
    category: 'Directional/Neutral',
    type: 'Income',
    description: 'Buy a longer-dated option at one strike and sell a nearer-dated option at a different (usually OTM) strike, combining time decay income with a mild directional bias. Acts as a synthetic covered call when a LEAPS option is used as the long leg, generating recurring income on trending stocks.',
    legs: 2,

    ivr_min: null, ivr_max: 50,
    rsi_signal: 'neutral',
    trend_requirement: 'above_200sma',
    iv_regime: 'any',
    entry_signal: 'Buy a back-month option 60–90 DTE and sell a front-month OTM option against it; target net debit ≤ 75% of strike spread.',

    delta_target: '0.30',
    delta_range: [0.15, 0.50],
    theta_bias: 'positive',
    vega_bias: 'positive',
    gamma_bias: 'negative',

    risk_type: 'defined',
    max_gain: 'Strike difference minus net debit (when stock is at short strike at front expiration)',
    max_loss: 'Net debit paid',
    breakeven: 'Long strike plus net debit minus total credits collected',

    profit_target_pct: 25,
    stop_loss_rule: 'Close if loss exceeds 50% of net debit',
    dte_entry: 60,
    dte_exit: 21,
    rolling_rule: 'Roll the short strike forward monthly at 21 DTE; adjust strike direction with stock movement',

    market_condition: 'Mildly directional trending stock with low to moderate IV',
    earnings_play: false,

    capital_pct_max: 5,
    capital_tier: 'low',
    margin_required: false,

    compatible_views: ['bullish', 'neutral'],
    compatible_objectives: ['income', 'growth'],
    compatible_risk: ['defined_only'],
    compatible_horizons: ['swing', 'long_term'],
    compatible_greeks_pref: ['theta', 'delta'],
  },

  // ── 11. Poor Man's Covered Call (PMCC) ──────────────────────────────────
  {
    id: 'pmcc',
    name: "Poor Man's Covered Call",
    category: 'Bullish',
    type: 'Directional',
    description: "Buy a deep ITM LEAPS call as a stock substitute and sell shorter-dated OTM calls against it to generate recurring income — mimicking a covered call at a fraction of the capital required to own shares. Best suited to moderately bullish, trending stocks with the long delta significantly exceeding the short delta.",
    legs: 2,

    ivr_min: null, ivr_max: 50,
    rsi_signal: 'oversold',
    trend_requirement: 'above_200sma',
    iv_regime: 'any',
    entry_signal: 'Buy a LEAPS call with delta ≥ 0.70 and at least 180 DTE; sell a 30 DTE OTM call with delta ≤ 0.30 against it.',

    delta_target: '0.60',
    delta_range: [0.40, 0.75],
    theta_bias: 'neutral',
    vega_bias: 'positive',
    gamma_bias: 'neutral',

    risk_type: 'defined',
    max_gain: 'Strike difference minus net debit (if stock is called away at short strike)',
    max_loss: 'Net debit paid for LEAPS minus all credits collected',
    breakeven: 'LEAPS strike plus net debit minus total credits collected',

    profit_target_pct: 50,
    stop_loss_rule: 'Close LEAPS if underlying drops 15–20% from entry price',
    dte_entry: 180,
    dte_exit: 21,
    rolling_rule: 'Roll short call forward at 21 DTE for additional credit; adjust strike as stock moves',

    market_condition: 'Moderately bullish, trending stock with low to moderate IV',
    earnings_play: false,

    capital_pct_max: 10,
    capital_tier: 'low',
    margin_required: false,

    compatible_views: ['bullish'],
    compatible_objectives: ['income', 'growth'],
    compatible_risk: ['defined_only'],
    compatible_horizons: ['long_term'],
    compatible_greeks_pref: ['theta', 'delta'],
  },

  // ── 12. Long Call / Long Put ─────────────────────────────────────────────
  {
    id: 'long_call_put',
    name: 'Long Call / Long Put',
    category: 'Directional',
    type: 'Directional',
    description: 'Buy a call for bullish leverage or a put for bearish leverage, with loss strictly limited to the premium paid and theoretically unlimited profit potential. Best entered in low-IV environments before an anticipated directional catalyst. Simple and accessible — ideal for targeting high-conviction moves.',
    legs: 1,

    ivr_min: null, ivr_max: 35,
    rsi_signal: 'any',
    trend_requirement: 'any',
    iv_regime: 'low',
    entry_signal: 'Enter calls in low-IV uptrends after pullbacks to support; enter puts in low-IV downtrends after rallies to resistance.',

    delta_target: '±0.40',
    delta_range: [0.30, 0.70],
    theta_bias: 'negative',
    vega_bias: 'positive',
    gamma_bias: 'positive',

    risk_type: 'defined',
    max_gain: 'Unlimited (calls) / Strike price (puts)',
    max_loss: 'Premium paid',
    breakeven: 'Strike ± premium paid',

    profit_target_pct: 100,
    stop_loss_rule: 'Close if option loses 50% of its purchase price',
    dte_entry: 45,
    dte_exit: 21,
    rolling_rule: 'Roll to next expiration if conviction remains but theta decay is accelerating',

    market_condition: 'Strong directional trend expected with low current implied volatility',
    earnings_play: true,

    capital_pct_max: 3,
    capital_tier: 'low',
    margin_required: false,

    compatible_views: ['bullish', 'bearish'],
    compatible_objectives: ['growth'],
    compatible_risk: ['defined_only'],
    compatible_horizons: ['intraday', 'swing'],
    compatible_greeks_pref: ['delta', 'vega'],
  },

  // ── 13. Short Strangle ───────────────────────────────────────────────────
  {
    id: 'short_strangle',
    name: 'Short Strangle',
    category: 'Neutral',
    type: 'Volatility',
    description: 'Sell an OTM call and an OTM put simultaneously, collecting premium from both sides. Profits when the stock stays between the two short strikes and IV contracts. Undefined risk on both sides requires active management and margin approval — the higher-premium, higher-risk cousin of the Iron Condor.',
    legs: 2,

    ivr_min: 60, ivr_max: null,
    rsi_signal: 'neutral',
    trend_requirement: 'consolidating',
    iv_regime: 'high',
    entry_signal: 'Sell the 16-delta call and 16-delta put (1 standard deviation OTM) with 45 DTE when IVR > 60.',

    delta_target: 'Net 0',
    delta_range: [-0.10, 0.10],
    theta_bias: 'positive',
    vega_bias: 'negative',
    gamma_bias: 'negative',

    risk_type: 'undefined',
    max_gain: 'Net credit received',
    max_loss: 'Theoretically unlimited on both sides',
    breakeven: 'Short call + credit  /  Short put − credit',

    profit_target_pct: 50,
    stop_loss_rule: '2× initial credit; close at 21 DTE regardless of P&L',
    dte_entry: 45,
    dte_exit: 21,
    rolling_rule: 'Roll the tested side to the same delta OTM at the next expiration when challenged',

    market_condition: 'Range-bound with very high IVR; expecting significant IV contraction (IV crush)',
    earnings_play: true,

    capital_pct_max: 5,
    capital_tier: 'high',
    margin_required: true,

    compatible_views: ['neutral'],
    compatible_objectives: ['income'],
    compatible_risk: ['undefined'],
    compatible_horizons: ['swing'],
    compatible_greeks_pref: ['theta', 'negative_vega'],
  },

  // ── 14. Short Straddle ───────────────────────────────────────────────────
  {
    id: 'short_straddle',
    name: 'Short Straddle',
    category: 'Neutral',
    type: 'Volatility',
    description: 'Sell an ATM call and ATM put at the same strike, collecting maximum premium when the stock is pinned at the strike at expiration. Highest potential income of any neutral strategy, with unlimited undefined risk in both directions. Requires significant margin and active management — suitable only for experienced traders.',
    legs: 2,

    ivr_min: 70, ivr_max: null,
    rsi_signal: 'neutral',
    trend_requirement: 'consolidating',
    iv_regime: 'high',
    entry_signal: 'Sell the ATM straddle with 30 DTE when IVR > 70; ideal immediately after a high-IV catalyst when premium remains elevated.',

    delta_target: 'Net 0',
    delta_range: [-0.05, 0.05],
    theta_bias: 'positive',
    vega_bias: 'negative',
    gamma_bias: 'negative',

    risk_type: 'undefined',
    max_gain: 'Net credit received (stock exactly at strike at expiration)',
    max_loss: 'Theoretically unlimited on both sides',
    breakeven: 'Short strike ± net credit received',

    profit_target_pct: 25,
    stop_loss_rule: '2× initial credit; close at 21 DTE; delta-hedge if stock moves > 1 standard deviation',
    dte_entry: 30,
    dte_exit: 14,
    rolling_rule: 'Roll to new ATM if stock moves > 1 SD; add wings to convert to Iron Butterfly to cap risk',

    market_condition: 'Post-event IV spike; expecting rapid IV contraction back to historical baseline',
    earnings_play: true,

    capital_pct_max: 5,
    capital_tier: 'high',
    margin_required: true,

    compatible_views: ['neutral'],
    compatible_objectives: ['income'],
    compatible_risk: ['undefined'],
    compatible_horizons: ['swing'],
    compatible_greeks_pref: ['theta', 'negative_vega'],
  },

  // ── 15. The Wheel ────────────────────────────────────────────────────────
  {
    id: 'the_wheel',
    name: 'The Wheel',
    category: 'Neutral/Bullish',
    type: 'Income',
    description: 'Sell cash-secured puts on a stock you want to own at a discount; if assigned, sell covered calls at or above your cost basis until the shares are called away. A systematic, cyclical income strategy that generates consistent premium on fundamentally sound stocks you are genuinely willing to hold long-term.',
    legs: 1,

    ivr_min: 30, ivr_max: null,
    rsi_signal: 'oversold',
    trend_requirement: 'above_200sma',
    iv_regime: 'any',
    entry_signal: 'Sell a cash-secured put at a strike you are comfortable owning shares at, targeting 30 DTE and delta between 0.20–0.35.',

    delta_target: '0.25',
    delta_range: [0.15, 0.35],
    theta_bias: 'positive',
    vega_bias: 'negative',
    gamma_bias: 'negative',

    risk_type: 'undefined',
    max_gain: 'Total premium collected across the full cycle',
    max_loss: 'Strike price minus all premium collected (stock goes to zero)',
    breakeven: 'Put strike minus total premium collected',

    profit_target_pct: 50,
    stop_loss_rule: 'Roll down and out if stock drops > 8%; exit wheel if stock breaks below key structural support',
    dte_entry: 30,
    dte_exit: 21,
    rolling_rule: 'Roll put down and out for credit on decline; sell covered calls at or above cost basis after assignment',

    market_condition: 'Moderately bullish, fundamentally sound stock with IVR > 30',
    earnings_play: false,

    capital_pct_max: 20,
    capital_tier: 'medium',
    margin_required: false,

    compatible_views: ['neutral', 'bullish'],
    compatible_objectives: ['income', 'acquiring_stock'],
    compatible_risk: ['defined_wide', 'undefined'],
    compatible_horizons: ['long_term', 'swing'],
    compatible_greeks_pref: ['theta', 'delta'],
  },

];
