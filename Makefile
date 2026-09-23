CXX := g++
CXXFLAGS := -O3 -march=native -std=c++17 -pthread -Wall -Wextra -Wno-unused-parameter

all: batchtruss debug_bisect

batchtruss: src/main.cpp src/common.h src/graph.h src/truss_static.h src/fixpoint.h src/incremental.h src/batch.h src/stream.h
	$(CXX) $(CXXFLAGS) -o $@ src/main.cpp

debug_bisect: src/debug_bisect.cpp src/common.h src/graph.h src/truss_static.h src/stream.h
	$(CXX) $(CXXFLAGS) -o $@ src/debug_bisect.cpp

clean:
	rm -f batchtruss debug_bisect

.PHONY: all clean
