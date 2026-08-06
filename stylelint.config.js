export default {
  extends: ["stylelint-config-standard"],
  rules: {
    // Related declarations share a line here, which is what makes a rule readable as one
    // thing. Nesting depth is capped instead, since that is what turns a sheet into a maze.
    "declaration-block-single-line-max-declarations": null,
    "max-nesting-depth": 2,
    "selector-max-id": 0,
    "no-descending-specificity": null,
  },
}
