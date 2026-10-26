// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "VideoDHelper",
    platforms: [.macOS(.v14)],
    products: [
        .library(name: "VideoCore", targets: ["VideoCore"]),
        .executable(name: "VideoDHelper", targets: ["VideoDHelper"])
    ],
    targets: [
        .target(name: "VideoCore"),
        .executableTarget(name: "VideoDHelper", dependencies: ["VideoCore"]),
        .testTarget(name: "VideoCoreTests", dependencies: ["VideoCore"])
    ],
    swiftLanguageVersions: [.v5]
)
