// Command logtail is a production-grade log file follower with rotation
// handling and checkpoint-based resume support.
//
// Usage:
//
//	logtail [flags] <file1> [file2 ...]
//
// Flags:
//
//	--checkpoint-dir     Directory for checkpoint files (default: .logtail-checkpoints)
//	--poll-interval      Stat poll interval (default: 5s)
//	--flush-interval     Checkpoint flush interval (default: 5s)
//	--start-at-end       Start reading from end of file (tail -f behavior)
//	--max-line-size      Maximum line size in bytes (default: 65536)
//
// Signals:
//
//	SIGINT / SIGTERM: graceful shutdown with final checkpoint flush.
//
// Output:
//
//	Each log line is printed to stdout as:
//	  <file_path>\t<byte_offset>\t<line_text>
//
// This format is designed for easy consumption by the LogPilot Python
// pipeline via a subprocess or pipe.
package main

import (
	"context"
	"flag"
	"fmt"
	"os"
	"time"

	"github.com/Yanghh0220/loggazer/logtail/manager"
	"github.com/Yanghh0220/loggazer/logtail/tailer"
)

func main() {
	var (
		checkpointDir = flag.String("checkpoint-dir", ".logtail-checkpoints", "Directory for checkpoint files")
		pollInterval  = flag.Duration("poll-interval", 5*time.Second, "Stat poll interval")
		flushInterval = flag.Duration("flush-interval", 5*time.Second, "Checkpoint flush interval")
		startAtEnd    = flag.Bool("start-at-end", false, "Start reading from end of file")
		maxLineSize   = flag.Int("max-line-size", 64*1024, "Maximum line size in bytes")
	)
	flag.Parse()

	if flag.NArg() == 0 {
		fmt.Fprintf(os.Stderr, "Usage: logtail [flags] <file1> [file2 ...]\n")
		flag.PrintDefaults()
		os.Exit(1)
	}

	// Build file configurations.
	files := make([]tailer.Config, 0, flag.NArg())
	for _, path := range flag.Args() {
		startOffset := int64(0)
		if *startAtEnd {
			startOffset = -1
		}
		files = append(files, tailer.Config{
			Path:         path,
			PollInterval: *pollInterval,
			FlushInterval: *flushInterval,
			MaxLineSize:  *maxLineSize,
			StartOffset:  startOffset,
		})
	}

	// Create manager.
	mgr, err := manager.New(manager.Config{
		CheckpointDir:       *checkpointDir,
		Files:               files,
		GlobalPollInterval:  *pollInterval,
		GlobalFlushInterval: *flushInterval,
		ShutdownTimeout:     30 * time.Second,
	})
	if err != nil {
		fmt.Fprintf(os.Stderr, "logtail: %v\n", err)
		os.Exit(1)
	}

	// Consume merged lines and print to stdout.
	// Format: <file_path>\t<byte_offset>\t<line_text>
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	go func() {
		for line := range mgr.Lines() {
			fmt.Printf("%s\t%d\t%s", line.Path, line.Offset, line.Text)
		}
	}()

	// Print stats periodically.
	go func() {
		ticker := time.NewTicker(30 * time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-ticker.C:
				s := mgr.Stats()
				fmt.Fprintf(os.Stderr, "# stats: files=%d rotations=%d bytes=%d errors=%d\n",
					s.Files, s.Rotations, s.BytesRead, s.Errors)
			case <-ctx.Done():
				return
			}
		}
	}()

	// Run (blocks until signal or error).
	if err := mgr.Run(ctx); err != nil {
		fmt.Fprintf(os.Stderr, "logtail: %v\n", err)
		os.Exit(1)
	}

	// Print final stats.
	s := mgr.Stats()
	fmt.Fprintf(os.Stderr, "# final: files=%d rotations=%d bytes=%d errors=%d\n",
		s.Files, s.Rotations, s.BytesRead, s.Errors)
}
