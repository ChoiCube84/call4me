import Foundation

/// User preferences. Only non-private settings live here.
/// Never store `ask_user` answers or typed call text in UserDefaults.
final class AppSettings: ObservableObject {
    static let shared = AppSettings()

    static let defaultDisclosure =
        "Hi, I'm an AI assistant calling on behalf of {name}. They are Deaf and are reading this call live."

    static var defaultBackendURL: String {
        infoValue("Call4meDefaultBackendURL") ?? "http://localhost:8000"
    }

    /// Access code baked in from Config/Secrets.xcconfig, if any.
    static var defaultAccessToken: String {
        infoValue("Call4meAccessToken") ?? ""
    }

    /// An Info.plist value, or nil when it is empty or an unexpanded `$(VAR)`
    /// (Secrets.xcconfig missing or the key left blank).
    private static func infoValue(_ key: String) -> String? {
        guard let raw = Bundle.main.object(forInfoDictionaryKey: key) as? String else { return nil }
        let value = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        if value.isEmpty || value.contains("$(") { return nil }
        return value
    }

    private let store = UserDefaults.standard

    @Published var name: String { didSet { store.set(name, forKey: Keys.name) } }
    @Published var disclosureTemplate: String { didSet { store.set(disclosureTemplate, forKey: Keys.disclosure) } }
    @Published var backendURL: String { didSet { store.set(backendURL, forKey: Keys.backend) } }
    /// The server's access code (API_TOKEN). Not user data; sent with every request.
    @Published var accessToken: String { didSet { store.set(accessToken, forKey: Keys.accessToken) } }
    @Published var hasOnboarded: Bool { didSet { store.set(hasOnboarded, forKey: Keys.onboarded) } }

    /// Anonymous id sent as `user_id`. Created once per install.
    let userId: String

    private init() {
        name = store.string(forKey: Keys.name) ?? ""
        disclosureTemplate = store.string(forKey: Keys.disclosure) ?? Self.defaultDisclosure
        backendURL = store.string(forKey: Keys.backend) ?? Self.defaultBackendURL
        accessToken = store.string(forKey: Keys.accessToken) ?? Self.defaultAccessToken
        hasOnboarded = store.bool(forKey: Keys.onboarded)
        if let id = store.string(forKey: Keys.userId) {
            userId = id
        } else {
            let id = UUID().uuidString.lowercased()
            store.set(id, forKey: Keys.userId)
            userId = id
        }
    }

    /// Disclosure with the user's name filled in.
    var disclosureText: String {
        let shownName = name.trimmingCharacters(in: .whitespaces).isEmpty ? "my client" : name
        return disclosureTemplate.replacingOccurrences(of: "{name}", with: shownName)
    }

    /// Trimmed access code, or nil when none is set.
    var token: String? {
        let t = accessToken.trimmingCharacters(in: .whitespacesAndNewlines)
        return t.isEmpty ? nil : t
    }

    var baseURL: URL? {
        URL(string: backendURL.trimmingCharacters(in: .whitespacesAndNewlines))
    }

    private enum Keys {
        static let name = "settings.name"
        static let disclosure = "settings.disclosureTemplate"
        static let backend = "settings.backendURL"
        static let accessToken = "settings.accessToken"
        static let onboarded = "settings.hasOnboarded"
        static let userId = "settings.userId"
    }
}
