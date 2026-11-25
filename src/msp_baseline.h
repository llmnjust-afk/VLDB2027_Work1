#pragma once
#include "graph.h"

std::vector<uint32_t> msp_truss(const DynGraph& graph, uint32_t threads,
                               double& preprocess_s, double& kernel_s);
