module memecoin-bot

go 1.21

require (
	github.com/gorilla/mux v1.8.1
	github.com/sirupsen/logrus v1.9.3
	github.com/spf13/viper v1.18.2
	gorm.io/driver/postgres v1.5.7
	gorm.io/gorm v1.25.10
)

// Run `go mod tidy` to populate go.sum and resolve the full dependency
// graph. go.sum is intentionally not committed here because it cannot be
// generated offline.
