// Package manager orchestrates multiple Tailer instances, providing
// multi-file support with isolated state, coordinated shutdown, and
// a single merged output channel.
package manager

import (
	"context"
	"fmt"
	"os"
	"os/signal"
	"sync"
	"syscall"
	"time"

	"github.com/Yanghh0220/loggazer/logtail/checkpoint"
	"github.com/Yanghh0220/loggazer/logtail/tailer"
)

// Config configures the multi-file log tailing manager.
type Config struct {
	// CheckpointDir is the directory where checkpoint files are stored.
	CheckpointDir string

	// Files is the list of file configurations to tail.
	Files []tailer.Config

	// GlobalPollInterval is the default poll interval applied to any
	// file config that has PollInterval <= 0.
	GlobalPollInterval time.Duration

	// GlobalFlushInterval is the default checkpoint flush interval.
	GlobalFlushInterval time.Duration

	// ShutdownTimeout is how long to wait for tailers to stop gracefully.
	ShutdownTimeout time.Duration
}

// Manager coordinates multiple tailers with a merged output channel.
type Manager struct {
	cfg    Config
	store  *checkpoint.Store
	lines  chan tailer.Line
	tailers []*tailer.Tailer
	wg     sync.WaitGroup
	mu     sync.Mutex
}

// New creates a new Manager. Call Run to start tailing.
func New(cfg Config) (*Manager, error) {
	if cfg.CheckpointDir == "" {
		cfg.CheckpointDir = ".logtail-checkpoints"
	}
	if cfg.GlobalPollInterval <= 0 {
		cfg.GlobalPollInterval = 5 * time.Second
	}
	if cfg.GlobalFlushInterval <= 0 {
		cfg.GlobalFlushInterval = 5 * time.Second
	}
	if cfg.ShutdownTimeout <= 0 {
		cfg.ShutdownTimeout = 30 * time.Second
	}

	store, err := checkpoint.NewStore(cfg.CheckpointDir)
	if err != nil {
		return nil, fmt.Errorf("manager: checkpoint store: %w", err)
	}

	// Apply global defaults to file configs.
	for i := range cfg.Files {
		if cfg.Files[i].PollInterval <= 0 {
			cfg.Files[i].PollInterval = cfg.GlobalPollInterval
		}
		if cfg.Files[i].FlushInterval <= 0 {
			cfg.Files[i].FlushInterval = cfg.GlobalFlushInterval
		}
	}

	return &Manager{
		cfg:   cfg,
		store: store,
		lines: make(chan tailer.Line, 1024),
	}, nil
}

// Lines returns the merged output channel for all tailed files.
func (m *Manager) Lines() <-chan tailer.Line { return m.lines }

// Run starts all tailers and blocks until a signal is received or ctx is cancelled.
// On return, all tailers have been shut down and checkpoints flushed.
func (m *Manager) Run(ctx context.Context) error {
	// Merge with OS signals for graceful shutdown.
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()

	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
	defer signal.Stop(sigCh)

	go func() {
		select {
		case <-sigCh:
			cancel()
		case <-ctx.Done():
		}
	}()

	// Start each tailer in its own goroutine.
	m.mu.Lock()
	for _, fc := range m.cfg.Files {
		t := tailer.New(fc, m.store)
		m.tailers = append(m.tailers, t)

		m.wg.Add(1)
		go func(tail *tailer.Tailer) {
			defer m.wg.Done()
			if err := tail.Run(ctx); err != nil && err != context.Canceled {
				// Log non-cancel errors (we can't import log here without circular deps,
				// so write to stderr in the CLI layer).
				fmt.Fprintf(os.Stderr, "tailer %s: %v\n", fc.Path, err)
			}
		}(t)
	}
	m.mu.Unlock()

	// Fan-in: merge all tailer channels into the manager's channel.
	m.wg.Add(1)
	go m.fanIn(ctx)

	// Block until cancelled.
	<-ctx.Done()

	// Graceful shutdown.
	m.shutdown()
	return nil
}

// fanIn merges lines from all tailers into the manager's output channel.
func (m *Manager) fanIn(ctx context.Context) {
	defer m.wg.Done()
	defer close(m.lines)

	// We can't easily merge N channels without reflect, so we'll
	// use a simpler approach: after all tailers are started, we
	// range over each tailer's channel sequentially. But this would
	// block on the first one. Instead, we use a fan-in pattern.

	// Since tailers are already started (Run called in goroutines),
	// we read from their channels here. We use a merge pattern.

	m.mu.Lock()
	tailers := make([]*tailer.Tailer, len(m.tailers))
	copy(tailers, m.tailers)
	m.mu.Unlock()

	if len(tailers) == 0 {
		return
	}

	// For each tailer, start a goroutine that forwards to m.lines.
	var fanWg sync.WaitGroup
	for _, t := range tailers {
		fanWg.Add(1)
		go func(tail *tailer.Tailer) {
			defer fanWg.Done()
			for line := range tail.Lines() {
				select {
				case m.lines <- line:
				case <-ctx.Done():
					return
				}
			}
		}(t)
	}
	fanWg.Wait()
}

// shutdown stops all tailers and flushes checkpoints.
func (m *Manager) shutdown() {
	m.mu.Lock()
	tailers := make([]*tailer.Tailer, len(m.tailers))
	copy(tailers, m.tailers)
	m.mu.Unlock()

	// Shut down each tailer with timeout.
	var shutdownWg sync.WaitGroup
	for _, t := range tailers {
		shutdownWg.Add(1)
		go func(tail *tailer.Tailer) {
			defer shutdownWg.Done()
			if err := tail.Shutdown(m.cfg.ShutdownTimeout); err != nil {
				fmt.Fprintf(os.Stderr, "manager: shutdown %s: %v\n", m.cfg.ShutdownTimeout, err)
			}
		}(t)
	}
	shutdownWg.Wait()

	// Wait for all Run goroutines to finish.
	m.wg.Wait()

	// Final checkpoint flush.
	if err := m.store.Close(); err != nil {
		fmt.Fprintf(os.Stderr, "manager: final checkpoint flush: %v\n", err)
	}
}

// Stats returns aggregate statistics across all tailers.
type Stats struct {
	Files     int
	Rotations int64
	BytesRead int64
	Errors    int64
}

// Stats returns aggregate statistics.
func (m *Manager) Stats() Stats {
	m.mu.Lock()
	defer m.mu.Unlock()

	var s Stats
	s.Files = len(m.tailers)
	for _, t := range m.tailers {
		s.Rotations += t.Rotations()
		s.BytesRead += t.BytesRead()
		s.Errors += t.Errors()
	}
	return s
}
