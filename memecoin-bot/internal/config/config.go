// Package config loads layered configuration: config.yaml for behaviour and
// thresholds, environment variables for secrets (keys, RPC URLs). Secrets are
// never read from the yaml file.
package config

import (
	"os"

	"github.com/spf13/viper"
)

type Config struct {
	Bot         BotConfig         `mapstructure:"bot"`
	Chains      ChainsConfig      `mapstructure:"chains"`
	Filters     FiltersConfig     `mapstructure:"filters"`
	Telegram    TelegramConfig    `mapstructure:"telegram"`
	KOLDatabase KOLDatabaseConfig `mapstructure:"kol_database"`
	Execution   ExecutionConfig   `mapstructure:"execution"`
	Dashboard   DashboardConfig   `mapstructure:"dashboard"`
	Database    DatabaseConfig    `mapstructure:"database"`
	Secrets     Secrets           `mapstructure:"-"`
}

type BotConfig struct {
	DryRun               bool `mapstructure:"dry_run"`
	AutoExecute          bool `mapstructure:"auto_execute"`
	MaxDailyLossSOL      float64 `mapstructure:"max_daily_loss_sol"`
	ConsecutiveLossLimit int  `mapstructure:"consecutive_loss_limit"`
}

type ChainsConfig struct {
	Solana SolanaConfig `mapstructure:"solana"`
	Base   BaseConfig   `mapstructure:"base"`
}

type SolanaConfig struct {
	RPCWs      string  `mapstructure:"rpc_ws"`
	PrivateRPC string  `mapstructure:"private_rpc"`
	MEVRelay   string  `mapstructure:"mev_relay"`
	JitoTipSOL float64 `mapstructure:"jito_tip_sol"`
}

type BaseConfig struct {
	RPCHTTP    string `mapstructure:"rpc_http"`
	PrivateRPC string `mapstructure:"private_rpc"`
}

type FiltersConfig struct {
	MinMarketCapUSD         float64 `mapstructure:"min_market_cap_usd"`
	MaxMarketCapUSD         float64 `mapstructure:"max_market_cap_usd"`
	MinVolume5mUSD          float64 `mapstructure:"min_volume_5m_usd"`
	MinHolders              int     `mapstructure:"min_holders"`
	WinProbabilityThreshold float64 `mapstructure:"win_probability_threshold"` // 0..1
}

type TelegramConfig struct {
	BotToken             string   `mapstructure:"bot_token"`
	MonitoredChannels    []string `mapstructure:"monitored_channels"`
	ConsolidatedChannelID int64   `mapstructure:"consolidated_channel_id"`
	RegexPattern         string   `mapstructure:"regex_pattern"`
}

type KOLDatabaseConfig struct {
	EnableTracking    bool     `mapstructure:"enable_tracking"`
	SnapshotIntervals []string `mapstructure:"snapshot_intervals"`
}

type ExecutionConfig struct {
	SlippageBuyPercent  float64         `mapstructure:"slippage_buy_percent"`
	SlippageSellPercent float64         `mapstructure:"slippage_sell_percent"`
	DefaultHoldSeconds  int             `mapstructure:"default_hold_seconds"`
	SellLadder          SellLadderConfig `mapstructure:"sell_ladder"`
}

type SellLadderConfig struct {
	Enabled bool             `mapstructure:"enabled"`
	Tiers   []SellLadderTier `mapstructure:"tiers"`
}

type SellLadderTier struct {
	ProfitMultiple float64 `mapstructure:"profit_multiple"`
	Percentage     int     `mapstructure:"percentage"`
}

type DashboardConfig struct {
	ListenAddress    string `mapstructure:"listen_address"`
	AuthUsername     string `mapstructure:"auth_username"`
	AuthPasswordHash string `mapstructure:"auth_password_hash"`
}

type DatabaseConfig struct {
	Host     string `mapstructure:"host"`
	Port     int    `mapstructure:"port"`
	User     string `mapstructure:"user"`
	Password string `mapstructure:"password"`
	DBName   string `mapstructure:"dbname"`
}

// Secrets are loaded only from the environment.
type Secrets struct {
	SolanaPrivateKey string
	BasePrivateKey   string
	RPCSolanaWS      string
	RPCBaseHTTP      string
	DeeznodeURL      string
	JitoAuthToken    string
	BloxrouteAPIKey  string
	JupiterAPI       string
	RugcheckAPI      string
	TelegramBotToken string
	TwitterBearer    string
}

func Load(path string) (*Config, error) {
	v := viper.New()
	v.SetConfigFile(path)
	setDefaults(v)
	if err := v.ReadInConfig(); err != nil {
		return nil, err
	}
	var c Config
	if err := v.Unmarshal(&c); err != nil {
		return nil, err
	}
	c.Secrets = loadSecrets()
	// Secrets override yaml placeholders where present.
	if c.Secrets.TelegramBotToken != "" {
		c.Telegram.BotToken = c.Secrets.TelegramBotToken
	}
	if c.Secrets.RPCSolanaWS != "" {
		c.Chains.Solana.RPCWs = c.Secrets.RPCSolanaWS
	}
	if c.Secrets.RPCBaseHTTP != "" {
		c.Chains.Base.RPCHTTP = c.Secrets.RPCBaseHTTP
	}
	if c.Database.Host == "" {
		c.Database = databaseFromEnv(c.Database)
	}
	return &c, nil
}

func setDefaults(v *viper.Viper) {
	v.SetDefault("bot.dry_run", true)
	v.SetDefault("bot.auto_execute", false)
	v.SetDefault("filters.win_probability_threshold", 0.80)
	v.SetDefault("execution.default_hold_seconds", 360)
	v.SetDefault("dashboard.listen_address", "0.0.0.0:8080")
	v.SetDefault("telegram.regex_pattern", `\b[1-9A-HJ-NP-Za-km-z]{32,44}\b`)
}

func loadSecrets() Secrets {
	return Secrets{
		SolanaPrivateKey: os.Getenv("SOLANA_PRIVATE_KEY"),
		BasePrivateKey:   os.Getenv("BASE_PRIVATE_KEY"),
		RPCSolanaWS:      os.Getenv("RPC_SOLANA_WS"),
		RPCBaseHTTP:      os.Getenv("RPC_BASE_HTTP"),
		DeeznodeURL:      os.Getenv("DEEZNODE_URL"),
		JitoAuthToken:    os.Getenv("JITO_AUTH_TOKEN"),
		BloxrouteAPIKey:  os.Getenv("BLOXROUTE_API_KEY"),
		JupiterAPI:       envDefault("JUPITER_API", "https://quote-api.jup.ag/v6"),
		RugcheckAPI:      envDefault("RUGCHECK_API", "https://api.rugcheck.xyz/v1"),
		TelegramBotToken: os.Getenv("TELEGRAM_BOT_TOKEN"),
		TwitterBearer:    os.Getenv("TWITTER_BEARER_TOKEN"),
	}
}

func databaseFromEnv(d DatabaseConfig) DatabaseConfig {
	d.Host = os.Getenv("DB_HOST")
	d.User = envDefault("DB_USER", "postgres")
	d.Password = envDefault("DB_PASSWORD", "postgres")
	d.DBName = envDefault("DB_NAME", "trading_bot")
	d.Port = 5432
	return d
}

// LiveTradingArmed reports whether configuration would permit real fund
// movement. The orchestrator uses this purely to refuse to start the live
// executor unless the operator has very explicitly opted in.
func (c *Config) LiveTradingArmed() bool {
	return !c.Bot.DryRun && c.Bot.AutoExecute && c.Secrets.SolanaPrivateKey != ""
}

func envDefault(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}
