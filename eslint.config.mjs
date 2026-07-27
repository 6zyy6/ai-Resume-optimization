import tsParser from "@typescript-eslint/parser";

export default [
  {
    ignores: ["**/dist/**", "packages/shared/generated/**"],
  },
  {
    files: ["**/*.ts"],
    languageOptions: { parser: tsParser },
  },
];
