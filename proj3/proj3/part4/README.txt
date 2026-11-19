Part 4: Cache-Aware Optimization of a Telemetry Analysis Program

Read the course Part 4 page for semantics, measurement commands and grading.

Build and check (Linux, g++ with C++17):
    make -j2
    make check
    ./bin/telemetry_student --pattern demo --csv health-report.csv

Default official input: 512 times, 64 devices, 16 channels, seed 123, pattern stress.
The four stages and four-tap arithmetic are fixed in telemetry.h.

Modify student.cc and add your own helper files if needed. Keep supplied input/
output semantics, arithmetic and compiler flags. If adding a .cc file, add it to
both the prerequisites and command of the student binary target only.
reference.cc, telemetry.h, driver.cc, part4_config.json and the reference build
are fixed course files. The grading driver checks every filtered value and every
report field. Native runtime is not the ChampSim performance score.

capture_trace.py captures only roi_begin -> roi_end, compresses the real trace,
records the exact original instruction count and appends 8192 unmeasured lookahead
records. Run ChampSim with warmup 0 and simulation length equal to roi_instructions
from the .xz.json metadata. Do not include padding in this length. The script does
not calculate cache metrics or performance scores.

Use the Pin installation prepared for the course. Build the included tracer with:
    make -C tracer PIN_ROOT="$PIN_ROOT"
The included tracer has start/stop symbol support and uses the 64-byte course
format. It includes bounded register recording and correct stop-symbol discovery. Pass their paths with --pin-root and --tracer. See the course page
for exact commands. Failure or hitting the instruction limit rejects the capture.

Only the input creation, driver verification and CSV export are outside the ROI.
All computation, intermediate allocation/initialization/copying and report building
must remain in analyze() and are measured. Preserve the input and all outputs.
The original code is supplied as the starting student.cc; no optimized answer is
included. The teacher's hidden semantic tests also exercise other input contents,
medium-sized dimensions and incomplete block/group tails.
