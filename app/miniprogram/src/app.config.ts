export default defineAppConfig({
  pages: [
    "pages/home/index",
    "pages/resumes/index",
    "pages/facts/index",
    "pages/me/index",
  ],
  subpackages: [
    { root: "subpackages/create", pages: ["index"] },
    { root: "subpackages/resume", pages: ["editor", "preview"] },
    { root: "subpackages/optimize", pages: ["import", "job", "match", "suggestions"] },
    { root: "subpackages/settings", pages: ["privacy"] },
  ],
  window: {
    backgroundColor: "#F8FAFC",
    backgroundTextStyle: "dark",
    navigationBarBackgroundColor: "#F8FAFC",
    navigationBarTextStyle: "black",
    navigationBarTitleText: "简历助手",
  },
  tabBar: {
    color: "#6B7280",
    selectedColor: "#4338CA",
    backgroundColor: "#FFFFFF",
    borderStyle: "black",
    list: [
      { pagePath: "pages/home/index", text: "首页" },
      { pagePath: "pages/resumes/index", text: "简历" },
      { pagePath: "pages/facts/index", text: "事实" },
      { pagePath: "pages/me/index", text: "我的" },
    ],
  },
});
