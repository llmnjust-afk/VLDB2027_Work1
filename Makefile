CXX := g++
CXXFLAGS := -O3 -march=native -std=c++17 -pthread -Wall -Wextra -Wno-unused-parameter

HEADERS := $(filter-out src/batch_v1_backup.h src/batch_mangled_trash.h,$(wildcard src/*.h))

all: batchtruss debug_bisect

batchtruss: src/main.cpp $(HEADERS)
	$(CXX) $(CXXFLAGS) -o $@ src/main.cpp

debug_bisect: src/debug_bisect.cpp $(HEADERS)
	$(CXX) $(CXXFLAGS) -o $@ src/debug_bisect.cpp

clean:
	rm -f batchtruss debug_bisect batchtruss-msp

.PHONY: all clean

# Optional published multi-stage peeling baseline, using the bundled source.
MSP := baselines/AccTrussDecomposition/msp
MSP_BUILD := .build/msp
MSP_CPPFLAGS := -I$(MSP)/GKlib -I$(MSP)/src -D__OPENMP__ -DUSE_LOG -DMSP_NO_HISTOGRAM -DLINUX -DHAVE_GETLINE -D_FILE_OFFSET_BITS=64
MSP_FLAGS := -O3 -march=native -fopenmp -DNDEBUG -DNDEBUG2
MSP_GK := $(patsubst $(MSP)/GKlib/%.c,$(MSP_BUILD)/GKlib/%.o,$(wildcard $(MSP)/GKlib/*.c))
MSP_C := $(patsubst $(MSP)/src/%.c,$(MSP_BUILD)/src/%.o,$(wildcard $(MSP)/src/*.c))
MSP_CPP := $(addprefix $(MSP_BUILD)/src/,kt.o multistage-peeling.o and.o yche/yche_helpers.o yche/log.o)

batchtruss-msp: src/main.cpp src/msp_baseline.cpp $(HEADERS) $(MSP_GK) $(MSP_C) $(MSP_CPP)
	$(CXX) $(CXXFLAGS) $(MSP_CPPFLAGS) -DHAVE_MSP -fopenmp -o $@ src/main.cpp src/msp_baseline.cpp $(MSP_GK) $(MSP_C) $(MSP_CPP) -lm

$(MSP_BUILD)/GKlib/%.o: $(MSP)/GKlib/%.c
	@mkdir -p "$(@D)"
	$(CC) $(MSP_CPPFLAGS) $(MSP_FLAGS) -std=gnu99 -fno-strict-aliasing -MMD -MP -c "$<" -o "$@"

$(MSP_BUILD)/src/%.o: $(MSP)/src/%.c
	@mkdir -p "$(@D)"
	$(CC) $(MSP_CPPFLAGS) $(MSP_FLAGS) -std=gnu99 -fno-strict-aliasing -MMD -MP -c "$<" -o "$@"

$(MSP_BUILD)/src/%.o: $(MSP)/src/%.cpp
	@mkdir -p "$(@D)"
	$(CXX) $(MSP_CPPFLAGS) $(MSP_FLAGS) -std=c++11 -MMD -MP -c "$<" -o "$@"

-include $(MSP_GK:.o=.d) $(MSP_C:.o=.d) $(MSP_CPP:.o=.d)
