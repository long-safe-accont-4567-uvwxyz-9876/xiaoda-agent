const path = require("path");
const electron = require("electron");

console.log("[Launcher] Loading LiteLoader...");
try {
    const liteLoader = require("./LiteLoader/src/main.js");
    console.log("[Launcher] LiteLoader loaded OK");
} catch (e) {
    console.error("[Launcher] LiteLoader error:", e.message);
}

console.log("[Launcher] Loading QQ...");
const main_path = "./application.asar/app_launcher/index.js";
require(path.join(process.resourcesPath, "app", main_path));
console.log("[Launcher] QQ loaded OK");
