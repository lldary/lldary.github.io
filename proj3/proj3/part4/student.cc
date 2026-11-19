#include "telemetry.h"

namespace telemetry {
// Starting implementation. Optimize this function while preserving the specified computation.
void analyze(const Input& input, Output& output)
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
