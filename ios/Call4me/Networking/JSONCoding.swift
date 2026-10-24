import Foundation

/// Shared encoders/decoders for the backend's snake_case JSON and
/// Python ISO 8601 timestamps (with or without fractional seconds / zone).
enum JSONCoding {
    /// For REST responses (typed models, snake_case keys converted).
    static let decoder: JSONDecoder = makeDecoder(convertKeys: true)

    /// For live messages. Keys are NOT converted, because key conversion also
    /// rewrites dictionary keys (e.g. `data["request_id"]`).
    static let liveDecoder: JSONDecoder = makeDecoder(convertKeys: false)

    private static func makeDecoder(convertKeys: Bool) -> JSONDecoder {
        let d = JSONDecoder()
        if convertKeys { d.keyDecodingStrategy = .convertFromSnakeCase }
        d.dateDecodingStrategy = .custom { decoder in
            let c = try decoder.singleValueContainer()
            let raw = try c.decode(String.self)
            if let date = parseDate(raw) { return date }
            throw DecodingError.dataCorruptedError(in: c, debugDescription: "Bad date: \(raw)")
        }
        return d
    }

    static let encoder: JSONEncoder = {
        let e = JSONEncoder()
        e.keyEncodingStrategy = .convertToSnakeCase
        return e
    }()

    static func parseDate(_ raw: String) -> Date? {
        let withFraction = ISO8601DateFormatter()
        withFraction.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let plain = ISO8601DateFormatter()
        plain.formatOptions = [.withInternetDateTime]
        // Python sends microseconds (6 digits); keep 3 for the formatter.
        let trimmed = raw.replacingOccurrences(
            of: #"(\.\d{3})\d+"#, with: "$1", options: .regularExpression)
        // Python may omit the zone for naive datetimes; treat those as UTC.
        let candidates = [trimmed, trimmed + "Z"]
        for s in candidates {
            if let d = withFraction.date(from: s) ?? plain.date(from: s) { return d }
        }
        return nil
    }
}
