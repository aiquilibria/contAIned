package docker

import (
	"os"
	"strings"
)

// ParseEnvFile reads a .env file and returns a map of key→value pairs.
//
// Handles the conventions that shell .env files use but that Docker's
// --env-file flag does not understand:
//   - Lines starting with # (comments) and blank lines are ignored.
//   - An "export " prefix is stripped (e.g. "export KEY=value").
//   - Values wrapped in matching single or double quotes have those quotes
//     stripped (e.g. KEY="value" → value).
func ParseEnvFile(path string) (map[string]string, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}

	result := make(map[string]string)
	for _, rawLine := range strings.Split(string(data), "\n") {
		line := strings.TrimSpace(rawLine)

		// Skip blank lines and comments.
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}

		// Strip optional 'export ' prefix.
		line = strings.TrimPrefix(line, "export ")
		line = strings.TrimSpace(line)

		// Must contain '=' to be a valid assignment.
		eq := strings.IndexByte(line, '=')
		if eq < 0 {
			continue
		}

		key := strings.TrimSpace(line[:eq])
		if key == "" {
			continue
		}
		value := line[eq+1:]

		// Strip matching surrounding quotes from the value.
		if len(value) >= 2 {
			if (value[0] == '"' && value[len(value)-1] == '"') ||
				(value[0] == '\'' && value[len(value)-1] == '\'') {
				value = value[1 : len(value)-1]
			}
		}

		result[key] = value
	}

	return result, nil
}
