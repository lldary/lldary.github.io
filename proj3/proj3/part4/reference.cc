#include "telemetry.h"
#include <cstdlib>

namespace telemetry {
void summarize(const Input& input, Output& output)
{
  const std::size_t groups = (input.devices + group_size - 1) / group_size;
  for (std::size_t t = first_output; t < input.times; ++t) {
    for (std::size_t g = 0; g < groups; ++g) {
      Health result;
      for (std::size_t d = g * group_size;
           d < std::min(input.devices, (g + 1) * group_size); ++d) {
        std::int32_t risk = 0;
        for (std::size_t c = 0; c < channels; ++c) {
          const auto value = output.filtered[((t - first_output) * input.devices + d) * channels + c];
          risk += std::abs(value);
        }
        result.total_risk += risk;
        result.alert_devices += risk > std::int32_t(600 * channels + (19 * d) % (400 * channels));
        result.peak_risk = std::max(result.peak_risk, risk);
      }
      output.report[(t - first_output) * groups + g] = result;
    }
  }
}

// Original offline pipeline: each channel processes the complete fleet.
void reference_analyze(const Input& input, Output& output)
{
  const std::size_t stride = input.devices * channels;
  std::vector<std::int32_t> a(input.times * stride), b(input.times * stride);
  const std::int32_t* source = input.data.data();
  std::int32_t* destination = a.data();
  for (std::size_t s = 0; s < stages; ++s) {
    for (std::size_t c = 0; c < channels; ++c) {
      for (std::size_t d = 0; d < input.devices; ++d) {
        for (std::size_t t = (s + 1) * (history - 1); t < input.times; ++t) {
          const std::size_t index = t * stride + d * channels + c;
          destination[index] = condition(source[index], source[index - stride],
                                         source[index - 2 * stride], source[index - 3 * stride], s, c);
        }
      }
    }
    source = destination;
    destination = destination == a.data() ? b.data() : a.data();
  }
  std::copy(source + first_output * stride, source + input.times * stride, output.filtered.begin());
  summarize(input, output);
}
} // namespace telemetry
