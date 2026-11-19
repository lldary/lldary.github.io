CXX = g++
CXXFLAGS = -std=c++17 -O2 -fno-tree-vectorize -fno-tree-slp-vectorize -fno-unroll-loops -fno-pie -no-pie -Wall -Wextra -Werror

.PHONY: all check clean
all: bin/telemetry_reference bin/telemetry_student

bin:
	mkdir -p bin

bin/telemetry_reference: driver.cc reference.cc telemetry.h Makefile | bin
	$(CXX) $(CXXFLAGS) -DTELEMETRY_REFERENCE_EXECUTABLE driver.cc reference.cc -o $@

bin/telemetry_student: driver.cc reference.cc student.cc telemetry.h Makefile | bin
	$(CXX) $(CXXFLAGS) driver.cc reference.cc student.cc -o $@

check: bin/telemetry_student
	./bin/telemetry_student --check

clean:
	$(RM) bin/telemetry_reference bin/telemetry_student
