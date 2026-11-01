// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "VideoDHelper",
    platforms: [.macOS(.v14)],
    products: [
        .library(name: "VideoCore", targets: ["VideoCore"]),
        .executable(name: "VideoDHelper", targets: ["VideoDHelper"]),
        .executable(name: "recording-diarize", targets: ["SpeakerHelper"])
    ],
    dependencies: [
        .package(url: "https://github.com/argmaxinc/argmax-oss-swift.git", revision: "9f99b5cb6a907730543823ad271b82532782a899")
    ],
    targets: [
        .target(name: "VideoCore"),
        .executableTarget(name: "VideoDHelper", dependencies: ["VideoCore"]),
        .executableTarget(name: "SpeakerHelper", dependencies: [
            .product(name: "SpeakerKit", package: "argmax-oss-swift")
        ]),
        .testTarget(name: "VideoCoreTests", dependencies: ["VideoCore"])
    ],
    swiftLanguageVersions: [.v5]
)
