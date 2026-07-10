CC ?= cc
CFLAGS ?= -O3 -Wall -Wextra -fwrapv
PYTHON ?= python3

CUBIOMES_DIR := vendor/cubiomes
CUBIOMES_LIB := $(CUBIOMES_DIR)/libcubiomes.a
MC_QUERY := native/mc_query

.PHONY: all native test clean

all: native

native: $(MC_QUERY)

$(CUBIOMES_LIB):
	$(MAKE) -C $(CUBIOMES_DIR) release

$(MC_QUERY): native/mc_query.c $(CUBIOMES_LIB)
	$(CC) $(CFLAGS) -I$(CUBIOMES_DIR) $< $(CUBIOMES_LIB) -lm -pthread -o $@

test: native
	$(PYTHON) -m unittest discover -s tests -v

clean:
	$(MAKE) -C $(CUBIOMES_DIR) clean
	$(RM) $(MC_QUERY)
