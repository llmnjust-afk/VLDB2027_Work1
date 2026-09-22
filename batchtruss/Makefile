CXX := g++
CXXFLAGS := -O3 -march=native -std=c++17 -pthread -Wall -Wextra -Wno-unused-parameter

batchtruss: src/main.cpp src/common.h src/graph.h src/truss_static.h src/fixpoint.h src/incremental.h src/batch.h src/stream.h
	$(CXX) $(CXXFLAGS) -o $@ src/main.cpp

clean:
	rm -f batchtruss

.PHONY: clean
