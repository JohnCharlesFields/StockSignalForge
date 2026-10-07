"""Bundled US-equity / ETF symbol seed for offline autocomplete.

This list ships with the application so that the single-stock search box works
instantly on a fresh deployment without ever calling a live quote/search API.
The seed is loaded once into the SQLite ``symbol_directory`` table; the table
then self-expands as the app discovers more symbols from snapshots / reports.

Format per line: ``SYMBOL|Company name|Exchange``
"""

from __future__ import annotations

from typing import Dict, List

_SEED_RAW = """
AAPL|Apple Inc.|NASDAQ
MSFT|Microsoft Corporation|NASDAQ
GOOGL|Alphabet Inc. Class A|NASDAQ
GOOG|Alphabet Inc. Class C|NASDAQ
AMZN|Amazon.com, Inc.|NASDAQ
NVDA|NVIDIA Corporation|NASDAQ
META|Meta Platforms, Inc.|NASDAQ
TSLA|Tesla, Inc.|NASDAQ
AVGO|Broadcom Inc.|NASDAQ
ORCL|Oracle Corporation|NYSE
ADBE|Adobe Inc.|NASDAQ
CRM|Salesforce, Inc.|NYSE
AMD|Advanced Micro Devices, Inc.|NASDAQ
INTC|Intel Corporation|NASDAQ
CSCO|Cisco Systems, Inc.|NASDAQ
QCOM|QUALCOMM Incorporated|NASDAQ
TXN|Texas Instruments Incorporated|NASDAQ
IBM|International Business Machines|NYSE
NOW|ServiceNow, Inc.|NYSE
INTU|Intuit Inc.|NASDAQ
AMAT|Applied Materials, Inc.|NASDAQ
MU|Micron Technology, Inc.|NASDAQ
ADI|Analog Devices, Inc.|NASDAQ
LRCX|Lam Research Corporation|NASDAQ
KLAC|KLA Corporation|NASDAQ
SNPS|Synopsys, Inc.|NASDAQ
CDNS|Cadence Design Systems, Inc.|NASDAQ
MRVL|Marvell Technology, Inc.|NASDAQ
PANW|Palo Alto Networks, Inc.|NASDAQ
CRWD|CrowdStrike Holdings, Inc.|NASDAQ
FTNT|Fortinet, Inc.|NASDAQ
ZS|Zscaler, Inc.|NASDAQ
DDOG|Datadog, Inc.|NASDAQ
NET|Cloudflare, Inc.|NYSE
SNOW|Snowflake Inc.|NYSE
PLTR|Palantir Technologies Inc.|NASDAQ
MDB|MongoDB, Inc.|NASDAQ
TEAM|Atlassian Corporation|NASDAQ
WDAY|Workday, Inc.|NASDAQ
ADSK|Autodesk, Inc.|NASDAQ
ANSS|ANSYS, Inc.|NASDAQ
ROP|Roper Technologies, Inc.|NASDAQ
MCHP|Microchip Technology Incorporated|NASDAQ
ON|ON Semiconductor Corporation|NASDAQ
NXPI|NXP Semiconductors N.V.|NASDAQ
SWKS|Skyworks Solutions, Inc.|NASDAQ
MPWR|Monolithic Power Systems, Inc.|NASDAQ
ENPH|Enphase Energy, Inc.|NASDAQ
FSLR|First Solar, Inc.|NASDAQ
SEDG|SolarEdge Technologies, Inc.|NASDAQ
SMCI|Super Micro Computer, Inc.|NASDAQ
ARM|Arm Holdings plc|NASDAQ
DELL|Dell Technologies Inc.|NYSE
HPQ|HP Inc.|NYSE
HPE|Hewlett Packard Enterprise|NYSE
WDC|Western Digital Corporation|NASDAQ
STX|Seagate Technology Holdings|NASDAQ
NTAP|NetApp, Inc.|NASDAQ
ANET|Arista Networks, Inc.|NYSE
KEYS|Keysight Technologies, Inc.|NYSE
GLW|Corning Incorporated|NYSE
APH|Amphenol Corporation|NYSE
TEL|TE Connectivity Ltd.|NYSE
NFLX|Netflix, Inc.|NASDAQ
DIS|The Walt Disney Company|NYSE
CMCSA|Comcast Corporation|NASDAQ
T|AT&T Inc.|NYSE
VZ|Verizon Communications Inc.|NYSE
TMUS|T-Mobile US, Inc.|NASDAQ
CHTR|Charter Communications, Inc.|NASDAQ
EA|Electronic Arts Inc.|NASDAQ
TTWO|Take-Two Interactive Software|NASDAQ
WBD|Warner Bros. Discovery, Inc.|NASDAQ
PARA|Paramount Global|NASDAQ
FOXA|Fox Corporation Class A|NASDAQ
OMC|Omnicom Group Inc.|NYSE
IPG|Interpublic Group|NYSE
PYPL|PayPal Holdings, Inc.|NASDAQ
SHOP|Shopify Inc.|NYSE
MELI|MercadoLibre, Inc.|NASDAQ
ABNB|Airbnb, Inc.|NASDAQ
UBER|Uber Technologies, Inc.|NYSE
LYFT|Lyft, Inc.|NASDAQ
DASH|DoorDash, Inc.|NASDAQ
BKNG|Booking Holdings Inc.|NASDAQ
EXPE|Expedia Group, Inc.|NASDAQ
ETSY|Etsy, Inc.|NASDAQ
EBAY|eBay Inc.|NASDAQ
PINS|Pinterest, Inc.|NYSE
SNAP|Snap Inc.|NYSE
RBLX|Roblox Corporation|NYSE
SPOT|Spotify Technology S.A.|NYSE
ROKU|Roku, Inc.|NASDAQ
ZM|Zoom Video Communications|NASDAQ
DOCU|DocuSign, Inc.|NASDAQ
OKTA|Okta, Inc.|NASDAQ
TWLO|Twilio Inc.|NYSE
COIN|Coinbase Global, Inc.|NASDAQ
HOOD|Robinhood Markets, Inc.|NASDAQ
SOFI|SoFi Technologies, Inc.|NASDAQ
AFRM|Affirm Holdings, Inc.|NASDAQ
UPST|Upstart Holdings, Inc.|NASDAQ
JPM|JPMorgan Chase & Co.|NYSE
BAC|Bank of America Corporation|NYSE
WFC|Wells Fargo & Company|NYSE
C|Citigroup Inc.|NYSE
GS|The Goldman Sachs Group, Inc.|NYSE
MS|Morgan Stanley|NYSE
BLK|BlackRock, Inc.|NYSE
SCHW|The Charles Schwab Corporation|NYSE
AXP|American Express Company|NYSE
USB|U.S. Bancorp|NYSE
PNC|The PNC Financial Services Group|NYSE
TFC|Truist Financial Corporation|NYSE
COF|Capital One Financial Corporation|NYSE
BK|The Bank of New York Mellon|NYSE
STT|State Street Corporation|NYSE
CB|Chubb Limited|NYSE
MMC|Marsh & McLennan Companies|NYSE
AON|Aon plc|NYSE
PGR|The Progressive Corporation|NYSE
TRV|The Travelers Companies, Inc.|NYSE
ALL|The Allstate Corporation|NYSE
MET|MetLife, Inc.|NYSE
PRU|Prudential Financial, Inc.|NYSE
AIG|American International Group|NYSE
AFL|Aflac Incorporated|NYSE
V|Visa Inc.|NYSE
MA|Mastercard Incorporated|NYSE
FI|Fiserv, Inc.|NYSE
GPN|Global Payments Inc.|NYSE
FIS|Fidelity National Information Services|NYSE
UNH|UnitedHealth Group Incorporated|NYSE
JNJ|Johnson & Johnson|NYSE
LLY|Eli Lilly and Company|NYSE
PFE|Pfizer Inc.|NYSE
MRK|Merck & Co., Inc.|NYSE
ABBV|AbbVie Inc.|NYSE
TMO|Thermo Fisher Scientific Inc.|NYSE
ABT|Abbott Laboratories|NYSE
DHR|Danaher Corporation|NYSE
BMY|Bristol-Myers Squibb Company|NYSE
AMGN|Amgen Inc.|NASDAQ
GILD|Gilead Sciences, Inc.|NASDAQ
CVS|CVS Health Corporation|NYSE
CI|The Cigna Group|NYSE
HUM|Humana Inc.|NYSE
ELV|Elevance Health, Inc.|NYSE
ZTS|Zoetis Inc.|NYSE
ISRG|Intuitive Surgical, Inc.|NASDAQ
MDT|Medtronic plc|NYSE
SYK|Stryker Corporation|NYSE
BSX|Boston Scientific Corporation|NYSE
BDX|Becton, Dickinson and Company|NYSE
EW|Edwards Lifesciences Corporation|NYSE
REGN|Regeneron Pharmaceuticals|NASDAQ
VRTX|Vertex Pharmaceuticals|NASDAQ
MRNA|Moderna, Inc.|NASDAQ
BIIB|Biogen Inc.|NASDAQ
IDXX|IDEXX Laboratories, Inc.|NASDAQ
DXCM|DexCom, Inc.|NASDAQ
ALGN|Align Technology, Inc.|NASDAQ
HCA|HCA Healthcare, Inc.|NYSE
MCK|McKesson Corporation|NYSE
CAH|Cardinal Health, Inc.|NYSE
PG|The Procter & Gamble Company|NYSE
KO|The Coca-Cola Company|NYSE
PEP|PepsiCo, Inc.|NASDAQ
COST|Costco Wholesale Corporation|NASDAQ
WMT|Walmart Inc.|NYSE
MDLZ|Mondelez International, Inc.|NASDAQ
CL|Colgate-Palmolive Company|NYSE
KMB|Kimberly-Clark Corporation|NYSE
GIS|General Mills, Inc.|NYSE
KHC|The Kraft Heinz Company|NASDAQ
MO|Altria Group, Inc.|NYSE
PM|Philip Morris International|NYSE
STZ|Constellation Brands, Inc.|NYSE
KDP|Keurig Dr Pepper Inc.|NASDAQ
MNST|Monster Beverage Corporation|NASDAQ
HSY|The Hershey Company|NYSE
K|Kellanova|NYSE
SYY|Sysco Corporation|NYSE
ADM|Archer-Daniels-Midland Company|NYSE
KR|The Kroger Co.|NYSE
DG|Dollar General Corporation|NYSE
DLTR|Dollar Tree, Inc.|NASDAQ
HD|The Home Depot, Inc.|NYSE
LOW|Lowe's Companies, Inc.|NYSE
MCD|McDonald's Corporation|NYSE
SBUX|Starbucks Corporation|NASDAQ
NKE|NIKE, Inc.|NYSE
TJX|The TJX Companies, Inc.|NYSE
LULU|Lululemon Athletica Inc.|NASDAQ
ROST|Ross Stores, Inc.|NASDAQ
CMG|Chipotle Mexican Grill, Inc.|NYSE
ORLY|O'Reilly Automotive, Inc.|NASDAQ
AZO|AutoZone, Inc.|NYSE
YUM|Yum! Brands, Inc.|NYSE
MAR|Marriott International, Inc.|NASDAQ
HLT|Hilton Worldwide Holdings|NYSE
GM|General Motors Company|NYSE
F|Ford Motor Company|NYSE
RIVN|Rivian Automotive, Inc.|NASDAQ
LCID|Lucid Group, Inc.|NASDAQ
APTV|Aptiv PLC|NYSE
DHI|D.R. Horton, Inc.|NYSE
LEN|Lennar Corporation|NYSE
PHM|PulteGroup, Inc.|NYSE
NVR|NVR, Inc.|NYSE
BA|The Boeing Company|NYSE
CAT|Caterpillar Inc.|NYSE
DE|Deere & Company|NYSE
HON|Honeywell International Inc.|NASDAQ
GE|GE Aerospace|NYSE
MMM|3M Company|NYSE
UPS|United Parcel Service, Inc.|NYSE
FDX|FedEx Corporation|NYSE
LMT|Lockheed Martin Corporation|NYSE
RTX|RTX Corporation|NYSE
NOC|Northrop Grumman Corporation|NYSE
GD|General Dynamics Corporation|NYSE
UNP|Union Pacific Corporation|NYSE
CSX|CSX Corporation|NASDAQ
NSC|Norfolk Southern Corporation|NYSE
EMR|Emerson Electric Co.|NYSE
ETN|Eaton Corporation plc|NYSE
ITW|Illinois Tool Works Inc.|NYSE
PH|Parker-Hannifin Corporation|NYSE
ROK|Rockwell Automation, Inc.|NYSE
CMI|Cummins Inc.|NYSE
PCAR|PACCAR Inc|NASDAQ
GWW|W.W. Grainger, Inc.|NYSE
FAST|Fastenal Company|NASDAQ
WM|Waste Management, Inc.|NYSE
RSG|Republic Services, Inc.|NYSE
DAL|Delta Air Lines, Inc.|NYSE
UAL|United Airlines Holdings|NASDAQ
AAL|American Airlines Group|NASDAQ
LUV|Southwest Airlines Co.|NYSE
XOM|Exxon Mobil Corporation|NYSE
CVX|Chevron Corporation|NYSE
COP|ConocoPhillips|NYSE
SLB|Schlumberger Limited|NYSE
EOG|EOG Resources, Inc.|NYSE
MPC|Marathon Petroleum Corporation|NYSE
PSX|Phillips 66|NYSE
VLO|Valero Energy Corporation|NYSE
OXY|Occidental Petroleum Corporation|NYSE
WMB|The Williams Companies, Inc.|NYSE
KMI|Kinder Morgan, Inc.|NYSE
OKE|ONEOK, Inc.|NYSE
HES|Hess Corporation|NYSE
DVN|Devon Energy Corporation|NYSE
FANG|Diamondback Energy, Inc.|NASDAQ
HAL|Halliburton Company|NYSE
BKR|Baker Hughes Company|NASDAQ
LNG|Cheniere Energy, Inc.|NYSE
LIN|Linde plc|NASDAQ
APD|Air Products and Chemicals|NYSE
SHW|The Sherwin-Williams Company|NYSE
ECL|Ecolab Inc.|NYSE
FCX|Freeport-McMoRan Inc.|NYSE
NEM|Newmont Corporation|NYSE
NUE|Nucor Corporation|NYSE
DOW|Dow Inc.|NYSE
DD|DuPont de Nemours, Inc.|NYSE
PPG|PPG Industries, Inc.|NYSE
VMC|Vulcan Materials Company|NYSE
MLM|Martin Marietta Materials|NYSE
ALB|Albemarle Corporation|NYSE
NEE|NextEra Energy, Inc.|NYSE
DUK|Duke Energy Corporation|NYSE
SO|The Southern Company|NYSE
D|Dominion Energy, Inc.|NYSE
AEP|American Electric Power|NASDAQ
EXC|Exelon Corporation|NASDAQ
SRE|Sempra|NYSE
XEL|Xcel Energy Inc.|NASDAQ
ED|Consolidated Edison, Inc.|NYSE
PEG|Public Service Enterprise Group|NYSE
WEC|WEC Energy Group, Inc.|NYSE
AEE|Ameren Corporation|NYSE
AMT|American Tower Corporation|NYSE
PLD|Prologis, Inc.|NYSE
CCI|Crown Castle Inc.|NYSE
EQIX|Equinix, Inc.|NASDAQ
PSA|Public Storage|NYSE
O|Realty Income Corporation|NYSE
SPG|Simon Property Group, Inc.|NYSE
WELL|Welltower Inc.|NYSE
DLR|Digital Realty Trust, Inc.|NYSE
VICI|VICI Properties Inc.|NYSE
SBAC|SBA Communications Corporation|NASDAQ
AVB|AvalonBay Communities, Inc.|NYSE
EQR|Equity Residential|NYSE
BABA|Alibaba Group Holding Limited|NYSE
PDD|PDD Holdings Inc.|NASDAQ
JD|JD.com, Inc.|NASDAQ
BIDU|Baidu, Inc.|NASDAQ
NIO|NIO Inc.|NYSE
LI|Li Auto Inc.|NASDAQ
XPEV|XPeng Inc.|NYSE
BILI|Bilibili Inc.|NASDAQ
TCOM|Trip.com Group Limited|NASDAQ
NTES|NetEase, Inc.|NASDAQ
TME|Tencent Music Entertainment|NYSE
FUTU|Futu Holdings Limited|NASDAQ
BEKE|KE Holdings Inc.|NYSE
YUMC|Yum China Holdings, Inc.|NYSE
BRK.B|Berkshire Hathaway Inc. Class B|NYSE
MSTR|MicroStrategy Incorporated|NASDAQ
GME|GameStop Corp.|NYSE
AMC|AMC Entertainment Holdings|NYSE
DKNG|DraftKings Inc.|NASDAQ
CVNA|Carvana Co.|NYSE
W|Wayfair Inc.|NYSE
CHWY|Chewy, Inc.|NYSE
PTON|Peloton Interactive, Inc.|NASDAQ
RKLB|Rocket Lab USA, Inc.|NASDAQ
ASTS|AST SpaceMobile, Inc.|NASDAQ
IONQ|IonQ, Inc.|NYSE
CELH|Celsius Holdings, Inc.|NASDAQ
DDOG|Datadog, Inc.|NASDAQ
ABNB|Airbnb, Inc.|NASDAQ
SPY|SPDR S&P 500 ETF Trust|NYSE Arca
QQQ|Invesco QQQ Trust|NASDAQ
IWM|iShares Russell 2000 ETF|NYSE Arca
DIA|SPDR Dow Jones Industrial Average ETF|NYSE Arca
VOO|Vanguard S&P 500 ETF|NYSE Arca
VTI|Vanguard Total Stock Market ETF|NYSE Arca
IVV|iShares Core S&P 500 ETF|NYSE Arca
VEA|Vanguard FTSE Developed Markets ETF|NYSE Arca
VWO|Vanguard FTSE Emerging Markets ETF|NYSE Arca
EFA|iShares MSCI EAFE ETF|NYSE Arca
EEM|iShares MSCI Emerging Markets ETF|NYSE Arca
XLK|Technology Select Sector SPDR Fund|NYSE Arca
XLF|Financial Select Sector SPDR Fund|NYSE Arca
XLE|Energy Select Sector SPDR Fund|NYSE Arca
XLV|Health Care Select Sector SPDR Fund|NYSE Arca
XLY|Consumer Discretionary SPDR Fund|NYSE Arca
XLP|Consumer Staples Select Sector SPDR|NYSE Arca
XLI|Industrial Select Sector SPDR Fund|NYSE Arca
XLU|Utilities Select Sector SPDR Fund|NYSE Arca
XLB|Materials Select Sector SPDR Fund|NYSE Arca
XLRE|Real Estate Select Sector SPDR Fund|NYSE Arca
XLC|Communication Services SPDR Fund|NYSE Arca
SMH|VanEck Semiconductor ETF|NASDAQ
SOXX|iShares Semiconductor ETF|NASDAQ
SOXL|Direxion Daily Semiconductor Bull 3X|NYSE Arca
SOXS|Direxion Daily Semiconductor Bear 3X|NYSE Arca
TQQQ|ProShares UltraPro QQQ|NASDAQ
SQQQ|ProShares UltraPro Short QQQ|NASDAQ
TLT|iShares 20+ Year Treasury Bond ETF|NASDAQ
IEF|iShares 7-10 Year Treasury Bond ETF|NASDAQ
HYG|iShares iBoxx High Yield Corporate Bond|NYSE Arca
LQD|iShares iBoxx Investment Grade Bond|NYSE Arca
GLD|SPDR Gold Shares|NYSE Arca
SLV|iShares Silver Trust|NYSE Arca
USO|United States Oil Fund|NYSE Arca
UNG|United States Natural Gas Fund|NYSE Arca
ARKK|ARK Innovation ETF|NYSE Arca
XBI|SPDR S&P Biotech ETF|NYSE Arca
IBB|iShares Biotechnology ETF|NASDAQ
KRE|SPDR S&P Regional Banking ETF|NYSE Arca
XRT|SPDR S&P Retail ETF|NYSE Arca
ITB|iShares U.S. Home Construction ETF|NYSE Arca
JETS|U.S. Global Jets ETF|NYSE Arca
GDX|VanEck Gold Miners ETF|NYSE Arca
GDXJ|VanEck Junior Gold Miners ETF|NYSE Arca
VXX|iPath Series B S&P 500 VIX Futures|Cboe
UVXY|ProShares Ultra VIX Short-Term Futures|Cboe
SPXL|Direxion Daily S&P 500 Bull 3X|NYSE Arca
SPXS|Direxion Daily S&P 500 Bear 3X|NYSE Arca
UPRO|ProShares UltraPro S&P 500|NYSE Arca
TNA|Direxion Daily Small Cap Bull 3X|NYSE Arca
TZA|Direxion Daily Small Cap Bear 3X|NYSE Arca
BITO|ProShares Bitcoin Strategy ETF|NYSE Arca
IBIT|iShares Bitcoin Trust|NASDAQ
FBTC|Fidelity Wise Origin Bitcoin Fund|Cboe
"""


def load_symbol_seed() -> List[Dict[str, str]]:
    """Parse the bundled seed into directory rows (deduplicated by symbol)."""
    rows: Dict[str, Dict[str, str]] = {}
    for line in _SEED_RAW.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        symbol = parts[0].upper() if parts else ""
        if not symbol:
            continue
        name = parts[1] if len(parts) > 1 and parts[1] else symbol
        exchange = parts[2] if len(parts) > 2 else ""
        rows[symbol] = {
            "symbol": symbol,
            "name": name,
            "exchange": exchange,
            "source": "seed",
        }
    return list(rows.values())
