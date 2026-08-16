import js from "@eslint/js"
import globals from "globals"

export default [
  {
    // The config UI is the only JavaScript that ships. tools/ is development tooling, and
    // a virtualenv holds whatever the packages in it happen to ship.
    ignores: ["tools/**", "build/**", "dist/**", ".venv/**", "index.html"],
  },
  js.configs.recommended,
  {
    files: ["src/statsbadge/web/*.js"],
    languageOptions: {
      ecmaVersion: "latest",
      sourceType: "script",
      globals: globals.browser,
    },
    rules: {
      semi: ["error", "never"],
      // A line opening with a bracket or a template literal continues the one above it
      // where there is no semicolon to say otherwise.
      "no-unexpected-multiline": "error",
      quotes: ["error", "double", { avoidEscape: true }],
      "comma-dangle": ["error", "always-multiline"],
      // Two spaces a level, and a continuation lines up under whatever it continues, which
      // is how the Python here is written too. No line length: ruff does not check one either.
      indent: ["error", 2, {
        flatTernaryExpressions: true,
        ArrayExpression: "first",
        ObjectExpression: "first",
        CallExpression: { arguments: "first" },
        FunctionDeclaration: { parameters: "first" },
        FunctionExpression: { parameters: "first" },
        VariableDeclarator: "first",
      }],
      "no-var": "error",
      "prefer-const": "error",
      "prefer-template": "error",
      "object-shorthand": "error",
      "arrow-parens": ["error", "always"],
      eqeqeq: ["error", "smart"],
      "dot-notation": "error",
      "no-else-return": "error",
      "no-unused-vars": ["error", { caughtErrors: "none" }],
      curly: ["error", "multi-line"],
    },
  },
]
