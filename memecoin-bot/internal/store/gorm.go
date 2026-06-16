package store

import (
	"fmt"

	"memecoin-bot/internal/models"

	"gorm.io/driver/postgres"
	"gorm.io/gorm"
)

// GormStore persists to PostgreSQL. It implements the same Store interface as
// MemoryStore, so the orchestrator is agnostic to which one is in use.
type GormStore struct {
	db *gorm.DB
}

type PostgresConfig struct {
	Host     string
	Port     int
	User     string
	Password string
	DBName   string
}

func NewGormStore(cfg PostgresConfig) (*GormStore, error) {
	dsn := fmt.Sprintf(
		"host=%s port=%d user=%s password=%s dbname=%s sslmode=disable",
		cfg.Host, cfg.Port, cfg.User, cfg.Password, cfg.DBName,
	)
	db, err := gorm.Open(postgres.Open(dsn), &gorm.Config{})
	if err != nil {
		return nil, err
	}
	if err := db.AutoMigrate(&models.Trade{}, &models.KolCall{}); err != nil {
		return nil, err
	}
	return &GormStore{db: db}, nil
}

func (s *GormStore) SaveTrade(t models.Trade) error { return s.db.Create(&t).Error }

func (s *GormStore) ListTrades() ([]models.Trade, error) {
	var out []models.Trade
	err := s.db.Order("timestamp desc").Find(&out).Error
	return out, err
}

func (s *GormStore) SaveKolCall(c models.KolCall) (uint, error) {
	err := s.db.Create(&c).Error
	return c.ID, err
}

func (s *GormStore) UpdateKolCall(c models.KolCall) error { return s.db.Save(&c).Error }

func (s *GormStore) GetKolCall(id uint) (models.KolCall, bool, error) {
	var c models.KolCall
	err := s.db.First(&c, id).Error
	if err == gorm.ErrRecordNotFound {
		return c, false, nil
	}
	return c, err == nil, err
}

func (s *GormStore) KolStats(name string) (models.KolStats, error) {
	var calls []models.KolCall
	if err := s.db.Where("kol_name = ?", name).Find(&calls).Error; err != nil {
		return models.KolStats{}, err
	}
	var wins int
	var returns6 []float64
	for _, c := range calls {
		if c.WinLossFlag == "win" {
			wins++
		}
		if c.WinLossFlag != "open" {
			returns6 = append(returns6, c.Return6min)
		}
	}
	st := models.KolStats{KOLName: name, TotalCalls: len(calls), Wins: wins, MedianReturn6min: median(returns6)}
	if len(calls) > 0 {
		st.WinRate = float64(wins) / float64(len(calls))
	}
	return st, nil
}
