//go:build !linux

package execution

import ("context"; "time")

func Detect(_ context.Context, _ ProbeOptions) Snapshot {
	return Snapshot{Platform: PlatformUnknown, DetectedAtUnixMS: time.Now().UTC().UnixMilli(), ProbeVersion: "1", ObservationScope: "unknown", Evidence: []string{"runtime capability probing is supported only on Linux"}}
}
