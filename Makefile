# Makefile for Project JAI Native C Sandbox
CC ?= gcc
CFLAGS ?= -std=gnu99 -shared -fPIC -O3 -Wall -Wextra -Werror

SRC = native/jai_sandbox.c
LIB = src/security/libjai_sandbox.so
NATIVE_LIB = native/libjai_sandbox.so

.PHONY: all build clean

all: build

build:
	@mkdir -p src/security native
	$(CC) $(CFLAGS) -o $(LIB) $(SRC)
	@cp $(LIB) $(NATIVE_LIB)
	@echo "Successfully compiled $(LIB)"

clean:
	rm -f $(LIB) $(NATIVE_LIB)
