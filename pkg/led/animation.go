package led

import (
	"bufio"
	"fmt"
	"strconv"
	"strings"
	"time"
)

/*
$ cat /system/etc/led-resources/boot_0.animation
loop
88:0ff,03f,03f,03f,03f,03f,03f,03f,03f,03f,03f,03f
88:03f,0ff,03f,03f,03f,03f,03f,03f,03f,03f,03f,03f
88:03f,03f,0ff,03f,03f,03f,03f,03f,03f,03f,03f,03f
88:03f,03f,03f,0ff,03f,03f,03f,03f,03f,03f,03f,03f
88:03f,03f,03f,03f,0ff,03f,03f,03f,03f,03f,03f,03f
88:03f,03f,03f,03f,03f,0ff,03f,03f,03f,03f,03f,03f
88:03f,03f,03f,03f,03f,03f,0ff,03f,03f,03f,03f,03f
88:03f,03f,03f,03f,03f,03f,03f,0ff,03f,03f,03f,03f
88:03f,03f,03f,03f,03f,03f,03f,03f,0ff,03f,03f,03f
88:03f,03f,03f,03f,03f,03f,03f,03f,03f,0ff,03f,03f
88:03f,03f,03f,03f,03f,03f,03f,03f,03f,03f,0ff,03f
88:03f,03f,03f,03f,03f,03f,03f,03f,03f,03f,03f,0ff

*/

type Animation struct {
	Looped bool
	Steps  []AnimationStep
}

type AnimationStep struct {
	Duration  time.Duration
	LedConfig []Led
}

func ParseAnimation(animation string) (Animation, error) {
	anim := Animation{
		Steps: make([]AnimationStep, 0),
	}

	scanner := bufio.NewScanner(strings.NewReader(animation))

	for scanner.Scan() {
		line := scanner.Text()

		if line == "loop" {
			anim.Looped = true
			continue
		}

		if strings.Contains(line, ":") {
			// Found animation encoding
			// 88:03f,03f,03f,0ff,03f,03f,03f,03f,03f,03f,03f,03f

			lineSplit := strings.Split(line, ":")
			rawDurationStr := lineSplit[0]
			duration, err := time.ParseDuration(fmt.Sprintf("%sms", rawDurationStr))
			if err != nil {
				return Animation{}, err
			}

			ledConfig := make([]Led, 0)

			ledSplit := strings.Split(lineSplit[1], ",")
			for index, led := range ledSplit {
				// 03f
				if len(led) != 3 {
					continue
				}

				ledArr := []rune(led)
				rawR := string(ledArr[0]) + string(ledArr[0])
				r, err := strconv.ParseUint(rawR, 16, 8)
				if err != nil {
					return Animation{}, err
				}
				rawG := string(ledArr[1]) + string(ledArr[1])
				g, err := strconv.ParseUint(rawG, 16, 8)
				if err != nil {
					return Animation{}, err
				}
				rawB := string(ledArr[2]) + string(ledArr[2])
				b, err := strconv.ParseUint(rawB, 16, 8)
				if err != nil {
					return Animation{}, err
				}
				ledConfig = append(ledConfig, Led{
					ID: index,
					R:  uint8(r),
					G:  uint8(g),
					B:  uint8(b),
				})
			}

			anim.Steps = append(anim.Steps, AnimationStep{
				Duration:  duration,
				LedConfig: ledConfig,
			})
		}
	}

	return anim, scanner.Err()
}
