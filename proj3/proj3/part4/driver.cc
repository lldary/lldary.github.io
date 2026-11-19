#include "telemetry.h"
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <string>
#include <utility>

extern "C" __attribute__((noinline)) void roi_begin() { asm volatile("" ::: "memory"); }
extern "C" __attribute__((noinline)) void roi_end() { asm volatile("" ::: "memory"); }

namespace {
using namespace telemetry;
struct Options {
  std::size_t times = 512, devices = 64;
  std::uint64_t seed = 123;
  std::string pattern = "stress", csv;
  bool check = false;
};

Input generate(const Options& opt)
{
  Input input{opt.times, opt.devices, std::vector<std::int32_t>(opt.times * opt.devices * channels)};
  std::uint64_t state = opt.seed;
  auto random = [&]() { state ^= state << 13; state ^= state >> 7; state ^= state << 17; return state; };
  for (std::size_t t = 0; t < opt.times; ++t) {
    for (std::size_t d = 0; d < opt.devices; ++d) {
      for (std::size_t c = 0; c < channels; ++c) {
        std::int32_t value;
        if (opt.pattern == "stress") value = std::int32_t(random() % 20001) - 10000;
        else if (opt.pattern == "zero") value = 0;
        else if (opt.pattern == "positive") value = 10000;
        else if (opt.pattern == "negative") value = -10000;
        else if (opt.pattern == "alternating") value = (t % 2) ? -10000 : 10000;
        else if (opt.pattern == "step") value = t < opt.times / 2 ? -9999 : 9999;
        else if (opt.pattern == "impulse")
          value = (t == first_output && d == opt.devices - 1 && c == channels - 1) ? 10000 : 0;
        else if (opt.pattern == "demo") {
          value = std::int32_t(random() % 2001) - 1000;
          if (d % 9 == 0 && t >= 220 && t < 400 && c < 8) value += 6000;
          if (d % 13 == 0 && t >= 350 && c >= 8) value -= 8000;
          if (d % 7 == 0 && t % 83 == 0) value += 9000;
          value = std::clamp(value, -10000, 10000);
        } else throw std::runtime_error("unknown input pattern");
        input.data[(t * opt.devices + d) * channels + c] = value;
      }
    }
  }
  return input;
}

Output allocate(const Input& input)
{
  const auto count = input.times - first_output;
  return {std::vector<std::int32_t>(count * input.devices * channels),
          std::vector<Health>(count * ((input.devices + group_size - 1) / group_size))};
}

void verify(const Output& actual, const Output& expected)
{
  if (actual.filtered != expected.filtered || actual.report.size() != expected.report.size())
    throw std::runtime_error("filtered values or output dimensions differ from reference");
  for (std::size_t i = 0; i < actual.report.size(); ++i) {
    const auto& a = actual.report[i];
    const auto& e = expected.report[i];
    if (a.total_risk != e.total_risk || a.alert_devices != e.alert_devices || a.peak_risk != e.peak_risk)
      throw std::runtime_error("health report differs from reference at record " + std::to_string(i));
  }
}

void run_selected(const Input& input, Output& output)
{
#ifdef TELEMETRY_REFERENCE_EXECUTABLE
  reference_analyze(input, output);
#else
  analyze(input, output);
#endif
}

void checked_run(const Options& opt, bool trace, Output* saved = nullptr)
{
  const Input input = generate(opt);
  const Input original = input;
  Output output = allocate(input);
  if (trace) roi_begin();
  run_selected(input, output);
  if (trace) roi_end();
  if (input.data != original.data) throw std::runtime_error("the input must remain unchanged");
  Output expected = allocate(original);
  reference_analyze(original, expected);
  verify(output, expected);
  if (saved) *saved = std::move(output);
}

std::uint64_t checksum(const Output& output)
{
  std::uint64_t hash = 1469598103934665603ULL;
  auto add = [&](std::uint64_t value) { hash = (hash ^ value) * 1099511628211ULL; };
  for (const auto value : output.filtered) add(std::uint64_t(value));
  for (const auto& value : output.report) {
    add(std::uint64_t(value.total_risk)); add(value.alert_devices); add(std::uint64_t(value.peak_risk));
  }
  return hash;
}

void write_csv(const Options& opt, const Output& output)
{
  std::ofstream file(opt.csv);
  if (!file) throw std::runtime_error("cannot open report CSV");
  file << "sample_index,device_group,total_risk,alert_devices,peak_risk\n";
  const auto groups = (opt.devices + group_size - 1) / group_size;
  for (std::size_t t = first_output; t < opt.times; ++t) {
    for (std::size_t g = 0; g < groups; ++g) {
      const auto& report = output.report[(t - first_output) * groups + g];
      file << t << ',' << g << ',' << report.total_risk << ',' << report.alert_devices << ',' << report.peak_risk << '\n';
    }
  }
  if (!file) throw std::runtime_error("failed to write report CSV");
}
} // namespace

int main(int argc, char** argv)
{
  try {
    Options opt;
    for (int i = 1; i < argc; ++i) {
      const std::string arg = argv[i];
      if (arg == "--check") { opt.check = true; continue; }
      if (arg == "--help") {
        std::cout << "Options: --times T --devices D --seed N --pattern NAME --csv PATH --check\n";
        return 0;
      }
      if (i + 1 == argc) throw std::runtime_error("missing argument for " + arg);
      const std::string value = argv[++i];
      if (arg == "--times") opt.times = std::stoull(value);
      else if (arg == "--devices") opt.devices = std::stoull(value);
      else if (arg == "--seed") opt.seed = std::stoull(value);
      else if (arg == "--pattern") opt.pattern = value;
      else if (arg == "--csv") opt.csv = value;
      else throw std::runtime_error("unknown option: " + arg);
    }
    if (opt.times <= first_output || opt.times > 2048 || !opt.devices || opt.devices > 256 || !opt.seed)
      throw std::runtime_error("require 13 <= times <= 2048, 1 <= devices <= 256, and a nonzero seed");
    if (opt.check) {
      std::size_t checks = 0;
      for (const auto& shape : {std::pair<std::size_t, std::size_t>{13, 1}, {37, 17}, {64, 31}, {73, 7}, {127, 33}}) {
        for (const std::string pattern : {"stress", "zero", "positive", "negative", "alternating", "step", "impulse"}) {
          Options test;
          test.times = shape.first; test.devices = shape.second;
          test.seed = 123 + checks * 97; test.pattern = pattern;
          checked_run(test, false); ++checks;
        }
      }
      for (const auto& shape : {std::pair<std::size_t, std::size_t>{512, 64}, {528, 63}}) {
        Options test; test.times = shape.first; test.devices = shape.second; test.seed = 987654321;
        checked_run(test, false); ++checks;
      }
      std::cout << "{\"checks\":" << checks << ",\"passed\":true}\n";
      return 0;
    }
    Output output;
    checked_run(opt, true, &output);
    if (!opt.csv.empty()) write_csv(opt, output);
#ifdef TELEMETRY_REFERENCE_EXECUTABLE
    const char* implementation = "reference";
#else
    const char* implementation = "student";
#endif
    std::cout << "{\"implementation\":\"" << implementation << "\",\"times\":" << opt.times
              << ",\"devices\":" << opt.devices << ",\"channels\":16,\"stages\":4,\"history\":4,\"seed\":" << opt.seed
              << ",\"pattern\":\"" << opt.pattern << "\",\"checksum\":" << checksum(output) << ",\"verified\":true}\n";
  } catch (const std::exception& error) {
    std::cerr << error.what() << '\n'; return 1;
  }
}
