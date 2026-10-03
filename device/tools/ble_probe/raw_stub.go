//go:build !bench

package main

import "log"

func runRaw(int, int, int, bool, string, int, int, string) {
	log.Fatal("-raw needs a build with -tags bench")
}

func runList(int) {
	log.Fatal("-list needs a build with -tags bench")
}

func runGatt(string, int, int, int, bool, bool, bool, string) {
	log.Fatal("-connect needs a build with -tags bench")
}
