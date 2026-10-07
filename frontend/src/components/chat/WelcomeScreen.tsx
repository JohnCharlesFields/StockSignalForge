import { Bot, TrendingUp, Globe, Sparkles, Users, UserCircle2, NotebookPen } from "lucide-react";
import { useI18n } from "@/lib/i18n";

interface Example {
  title: string;
  desc: string;
  prompt: string;
}

interface Category {
  label: string;
  icon: React.ReactNode;
  color: string;
  examples: Example[];
}

function getCategories(lang: "en" | "zh"): Category[] {
  if (lang === "zh") {
    return [
      {
        label: "多市场回测",
        icon: <TrendingUp className="h-4 w-4" />,
        color: "text-red-400 border-red-500/30 hover:border-red-500/60 hover:bg-red-500/5",
        examples: [
          {
            title: "跨市场组合",
            desc: "A 股 + 加密资产 + 美股，使用风险平价优化",
            prompt: "回测由 000001.SZ、BTC-USDT 和 AAPL 组成的风险平价组合，区间为 2024 全年，并与等权组合基准对比",
          },
          {
            title: "BTC 5 分钟 MACD 策略",
            desc: "基于 OKX 实时数据的分钟级加密资产回测",
            prompt: "回测 BTC-USDT 5 分钟 MACD 策略，fast=12 slow=26 signal=9，区间为最近 30 天",
          },
          {
            title: "美股科技组合最大分散化",
            desc: "基于 yfinance 的 FAANG+ 组合优化",
            prompt: "回测 AAPL、MSFT、GOOGL、AMZN、NVDA 的最大分散化组合优化策略，区间为 2024 全年",
          },
        ],
      },
      {
        label: "投研与分析",
        icon: <Sparkles className="h-4 w-4" />,
        color: "text-amber-400 border-amber-500/30 hover:border-amber-500/60 hover:bg-amber-500/5",
        examples: [
          {
            title: "多因子 Alpha 模型",
            desc: "基于 IC 加权的动量、反转、波动率与换手率因子合成",
            prompt: "使用动量、反转、波动率和换手率因子，在沪深 300 成分股上构建 IC 加权多因子 Alpha 模型，并回测 2023-2024 年表现",
          },
          {
            title: "期权希腊值分析",
            desc: "Black-Scholes 定价与 Delta/Gamma/Theta/Vega 风险暴露",
            prompt: "使用 Black-Scholes 模型计算期权希腊值：现价=100，行权价=105，无风险利率=3%，波动率=25%，到期时间=90 天，并分析 Delta/Gamma/Theta/Vega",
          },
        ],
      },
      {
        label: "多智能体团队",
        icon: <Users className="h-4 w-4" />,
        color: "text-violet-400 border-violet-500/30 hover:border-violet-500/60 hover:bg-violet-500/5",
        examples: [
          {
            title: "投资委员会复核",
            desc: "多智能体辩论：多空观点、风险复核、投资经理决策",
            prompt: "[多智能体团队模式] 使用 investment_committee 预设，结合当前市场环境评估 NVDA 应该做多还是做空",
          },
          {
            title: "量化策略台",
            desc: "选股筛选 -> 因子研究 -> 回测 -> 风险审计流程",
            prompt: "[多智能体团队模式] 使用 quant_strategy_desk 预设，在沪深 300 成分股中寻找并回测最优动量策略",
          },
        ],
      },
      {
        label: "文档与网页投研",
        icon: <Globe className="h-4 w-4" />,
        color: "text-blue-400 border-blue-500/30 hover:border-blue-500/60 hover:bg-blue-500/5",
        examples: [
          {
            title: "分析财报 PDF",
            desc: "上传财报并提问关键财务指标、风险与展望",
            prompt: "总结我上传的财报中的核心财务指标、主要风险和管理层展望",
          },
          {
            title: "宏观网页研究",
            desc: "读取实时网页来源，形成宏观市场分析",
            prompt: "读取最新美联储会议纪要，并总结其对股票市场和加密资产市场的影响",
          },
        ],
      },
      {
        label: "交易日志",
        icon: <NotebookPen className="h-4 w-4" />,
        color: "text-orange-400 border-orange-500/30 hover:border-orange-500/60 hover:bg-orange-500/5",
        examples: [
          {
            title: "分析券商导出记录",
            desc: "解析同花顺/东财/富途/通用 CSV，统计持仓天数、胜率、盈亏比和日内分布",
            prompt: "分析我刚上传的交易日志，输出完整交易画像，包括持仓统计、胜率、主要标的和小时分布",
          },
          {
            title: "诊断交易行为偏差",
            desc: "处置效应、过度交易、追涨、锚定偏差，输出严重程度和量化证据",
            prompt: "对我的交易日志运行 4 类行为诊断：处置效应、过度交易、追涨和锚定，并指出哪类偏差对 PnL 伤害最大",
          },
        ],
      },
      {
        label: "影子账户",
        icon: <UserCircle2 className="h-4 w-4" />,
        color: "text-emerald-400 border-emerald-500/30 hover:border-emerald-500/60 hover:bg-emerald-500/5",
        examples: [
          {
            title: "从交易日志训练影子账户",
            desc: "从券商 CSV 中提取你的交易规则并保存影子账户画像",
            prompt: "根据我刚上传的交易日志训练影子账户，展示提取出的交易规则，并确认它们是否符合我的交易行为",
          },
          {
            title: "我错过了多少收益？",
            desc: "回测影子策略，并归因实际 PnL 与影子账户 PnL 的差异",
            prompt: "对最近 90 天美股市场运行影子账户回测，拆解我的实际 PnL 与影子账户 PnL 的差异来源，包括规则违背、过早平仓和错失信号",
          },
          {
            title: "生成影子账户报告",
            desc: "8 节 HTML/PDF 报告：权益曲线、分市场夏普比率、归因瀑布图",
            prompt: "渲染影子账户报告并给我 URL，开头先给出我与影子账户之间的收益差异",
          },
        ],
      },
    ];
  }

  return [
  {
    label: "Multi-Market Backtest",
    icon: <TrendingUp className="h-4 w-4" />,
    color: "text-red-400 border-red-500/30 hover:border-red-500/60 hover:bg-red-500/5",
    examples: [
      {
        title: "Cross-Market Portfolio",
        desc: "A-shares + crypto + US equities with risk-parity optimizer",
        prompt: "Backtest a risk-parity portfolio of 000001.SZ, BTC-USDT, and AAPL for full-year 2024, compare against equal-weight baseline",
      },
      {
        title: "BTC 5-Min MACD Strategy",
        desc: "Minute-level crypto backtest with real-time OKX data",
        prompt: "Backtest BTC-USDT 5-minute MACD strategy, fast=12 slow=26 signal=9, last 30 days",
      },
      {
        title: "US Tech Max Diversification",
        desc: "Portfolio optimizer across FAANG+ via yfinance",
        prompt: "Backtest AAPL, MSFT, GOOGL, AMZN, NVDA with max_diversification portfolio optimizer, full-year 2024",
      },
    ],
  },
  {
    label: "Research & Analysis",
    icon: <Sparkles className="h-4 w-4" />,
    color: "text-amber-400 border-amber-500/30 hover:border-amber-500/60 hover:bg-amber-500/5",
    examples: [
      {
        title: "Multi-Factor Alpha Model",
        desc: "IC-weighted factor synthesis across 300 stocks",
        prompt: "Build a multi-factor alpha model using momentum, reversal, volatility, and turnover on CSI 300 constituents with IC-weighted factor synthesis, backtest 2023-2024",
      },
      {
        title: "Options Greeks Analysis",
        desc: "Black-Scholes pricing with Delta/Gamma/Theta/Vega",
        prompt: "Calculate option Greeks using Black-Scholes: spot=100, strike=105, risk-free rate=3%, vol=25%, expiry=90 days, analyze Delta/Gamma/Theta/Vega",
      },
    ],
  },
  {
    label: "Swarm Teams",
    icon: <Users className="h-4 w-4" />,
    color: "text-violet-400 border-violet-500/30 hover:border-violet-500/60 hover:bg-violet-500/5",
    examples: [
      {
        title: "Investment Committee Review",
        desc: "Multi-agent debate: long vs short, risk review, PM decision",
        prompt: "[Swarm Team Mode] Use the investment_committee preset to evaluate whether to go long or short on NVDA given current market conditions",
      },
      {
        title: "Quant Strategy Desk",
        desc: "Screening → factor research → backtest → risk audit pipeline",
        prompt: "[Swarm Team Mode] Use the quant_strategy_desk preset to find and backtest the best momentum strategy on CSI 300 constituents",
      },
    ],
  },
  {
    label: "Document & Web Research",
    icon: <Globe className="h-4 w-4" />,
    color: "text-blue-400 border-blue-500/30 hover:border-blue-500/60 hover:bg-blue-500/5",
    examples: [
      {
        title: "Analyze an Earnings Report PDF",
        desc: "Upload a PDF and ask questions about the financials",
        prompt: "Summarize the key financial metrics, risks, and outlook from the uploaded earnings report",
      },
      {
        title: "Web Research: Macro Outlook",
        desc: "Read live web sources for macro analysis",
        prompt: "Read the latest Fed meeting minutes and summarize the key takeaways for equity and crypto markets",
      },
    ],
  },
  {
    label: "Trade Journal",
    icon: <NotebookPen className="h-4 w-4" />,
    color: "text-orange-400 border-orange-500/30 hover:border-orange-500/60 hover:bg-orange-500/5",
    examples: [
      {
        title: "Analyze My Broker Export",
        desc: "Parse 同花顺/东财/富途/generic CSV — holding days, win rate, PnL ratio, hourly distribution",
        prompt: "Analyze the trade journal I just uploaded — full profile with holding stats, win rate, top symbols, and hourly distribution",
      },
      {
        title: "Diagnose My Behavior Biases",
        desc: "Disposition effect, overtrading, chasing momentum, anchoring — severity + numeric evidence",
        prompt: "Run the 4 behavior diagnostics on my trade journal (disposition, overtrading, chasing, anchoring) and tell me which bias hurts my PnL most",
      },
    ],
  },
  {
    label: "Shadow Account",
    icon: <UserCircle2 className="h-4 w-4" />,
    color: "text-emerald-400 border-emerald-500/30 hover:border-emerald-500/60 hover:bg-emerald-500/5",
    examples: [
      {
        title: "Train My Shadow from Journal",
        desc: "Extract your strategy rules from a broker CSV and persist a Shadow profile",
        prompt: "Train my shadow account from the trading journal I just uploaded — show the extracted rules and confirm they look like my behavior",
      },
      {
        title: "How Much Am I Leaving on the Table?",
        desc: "Backtest your shadow strategy and attribute delta vs. your actual PnL",
        prompt: "Run a shadow backtest for the last 90 days on the US market and break down where my PnL diverged from the shadow (rule violations, early exits, missed signals)",
      },
      {
        title: "Generate Shadow Report",
        desc: "8-section HTML/PDF — equity curve, per-market Sharpe, attribution waterfall",
        prompt: "Render the shadow report and give me the URL — lead with the you-vs-shadow delta",
      },
    ],
  },
  ];
}

interface Props {
  onExample: (s: string) => void;
}

export function WelcomeScreen({ onExample }: Props) {
  const { t, lang } = useI18n();
  const categories = getCategories(lang);
  const capabilityChips = [
    t.capability70Skills,
    t.capability29Swarms,
    t.capability32Tools,
    t.capabilityMarkets,
    t.capabilityTimeframes,
    t.capabilityOptimizers,
    t.capabilityRiskMetrics,
    t.capabilityOptions,
    t.capabilityResearch,
    t.capabilityFactors,
    t.capabilityJournal,
    t.capabilityShadow,
    t.capabilityMemory,
    t.capabilitySearch,
  ];

  return (
    <div className="flex flex-col items-center justify-center min-h-[60vh] space-y-8 text-center">
      {/* Header */}
      <div className="space-y-3">
        <div className="relative h-16 w-16 mx-auto rounded-full border-2 border-amber-500/80 bg-card flex items-center justify-center shadow-lg">
          <span className="absolute h-12 w-12 rounded-full border border-amber-500/40" />
          <span className="relative text-lg font-black text-amber-500">35</span>
          <Bot className="absolute -bottom-1 -right-1 h-4 w-4 rounded-full bg-card p-0.5 text-primary" />
        </div>
        <div>
          <h2 className="text-2xl font-bold tracking-tight">easymoneysniper</h2>
          <p className="mt-1 text-[10px] font-semibold uppercase tracking-[0.2em] text-amber-500">research the gap · respect the evidence</p>
          <p className="text-xs text-muted-foreground mt-1 max-w-sm mx-auto leading-relaxed">
            {t.appTagline}
          </p>
          <p className="text-sm text-muted-foreground mt-2 max-w-md leading-relaxed mx-auto">
            {t.describeStrategy}
          </p>
        </div>
      </div>

      {/* Capability chips */}
      <div className="flex flex-wrap justify-center gap-2 max-w-lg">
        {capabilityChips.map((chip) => (
          <span
            key={chip}
            className="px-2.5 py-1 text-xs rounded-full border border-border/60 text-muted-foreground bg-muted/30"
          >
            {chip}
          </span>
        ))}
      </div>

      {/* Example categories grid */}
      <div className="w-full max-w-2xl text-left space-y-4">
        <p className="text-xs text-muted-foreground px-1">{t.examples}</p>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
          {categories.map((cat) => (
            <div key={cat.label} className="space-y-2">
              <div className={`flex items-center gap-1.5 text-xs font-medium px-1 ${cat.color.split(" ").filter(c => c.startsWith("text-")).join(" ")}`}>
                {cat.icon}
                <span>{cat.label}</span>
              </div>
              <div className="space-y-1.5">
                {cat.examples.map((ex) => (
                  <button
                    key={ex.title}
                    onClick={() => onExample(ex.prompt)}
                    className={`block w-full text-left px-3 py-2.5 rounded-xl border transition-colors ${cat.color}`}
                  >
                    <span className="text-sm font-medium text-foreground leading-snug">
                      {ex.title}
                    </span>
                    <span className="block text-xs text-muted-foreground mt-0.5 leading-snug">
                      {ex.desc}
                    </span>
                  </button>
                ))}
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
