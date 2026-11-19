#ifndef ARCHLAB_TELEMETRY_H
#define ARCHLAB_TELEMETRY_H

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <vector>

namespace telemetry {
constexpr std::size_t channels = 16;
constexpr std::size_t stages = 4;
constexpr std::size_t history = 4;
constexpr std::size_t first_output = stages * (history - 1);
constexpr std::size_t group_size = 16;

// Readings are normalized signed deviations, each in [-10000, 10000].
// Acquisition layout: data[(time * devices + device) * channels + channel].
struct Input {
  std::size_t times;
  std::size_t devices;
  std::vector<std::int32_t> data;
};

struct Health {
  std::int64_t total_risk = 0;
  std::uint64_t alert_devices = 0;
  std::int32_t peak_risk = 0;
};

struct Output {
  // filtered[((time - first_output) * devices + device) * channels + channel]
  std::vector<std::int32_t> filtered;
  // report[(time - first_output) * ceil(devices / group_size) + group]
  std::vector<Health> report;
};

// Supplied arithmetic primitive. Keep this calculation and each stage boundary.
inline std::int32_t condition(std::int32_t now, std::int32_t lag1,
                              std::int32_t lag2, std::int32_t lag3,
                              std::size_t stage, std::size_t channel)
{
  const std::int64_t weighted = std::int64_t(now) + 2 * std::int64_t(lag1)
                            + 3 * std::int64_t(lag2) + 4 * std::int64_t(lag3);
  const std::int64_t limit = 2400 - 100 * std::int64_t(stage)
                            + 50 * std::int64_t(channel % 4);
  // Signed integer division truncates toward zero, before clamping.
  return static_cast<std::int32_t>(std::clamp(weighted / 10, -limit, limit));
}

// Provided report stage; its work is included in the measured function.
void summarize(const Input& input, Output& output);
void reference_analyze(const Input& input, Output& output);
// Implement this function in student.cc. All optimization work belongs here.
void analyze(const Input& input, Output& output);
} // namespace telemetry
#endif
