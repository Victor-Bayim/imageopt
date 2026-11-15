package bootstrap

import (
	"fmt"
	"io"
	"os"
)

type ReadStats struct {
	Path      string
	BytesRead int64
	Checksum  uint64
}

func ReadPath(path string) (ReadStats, error) {
	file, err := os.Open(path)
	if err != nil {
		return ReadStats{}, fmt.Errorf("open %s: %w", path, err)
	}
	defer file.Close()

	buffer := make([]byte, ReadBufferSize)
	stats := ReadStats{Path: path}
	for {
		n, readErr := file.Read(buffer)
		if n > 0 {
			block := buffer[:n]
			stats.BytesRead += int64(n)
			stats.Checksum = updateChecksum(stats.Checksum, block)
		}
		if readErr == io.EOF {
			break
		}
		if readErr != nil {
			return ReadStats{}, fmt.Errorf("read %s: %w", path, readErr)
		}
	}
	return stats, nil
}

func updateChecksum(current uint64, block []byte) uint64 {
	if len(block) == 0 {
		return current
	}
	const stride = 4096
	sum := current
	for i := 0; i < len(block); i += stride {
		sum = sum*131 + uint64(block[i]) + uint64(i&0xFF)
	}
	mid := len(block) / 2
	sum = sum*131 + uint64(block[mid]) + uint64(mid&0xFF)
	last := len(block) - 1
	sum = sum*131 + uint64(block[last]) + uint64(last&0xFF)
	return sum
}
