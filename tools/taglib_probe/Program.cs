// A dumb reader. It prints what TagLib# reports for a file and interprets nothing.
//
// All of the mapping (bits/sec versus kbit/s, container naming, which tag wins) lives in the
// Python harness, next to the mirrored ffprobe mapping it is being compared against. Keeping
// the two mappings side by side in one file is what makes them auditable; a probe that
// pre-massaged its own side would hide half the comparison in C#.
//
// Paths arrive on stdin, one per line, rather than on argv: a real library can be tens of
// thousands of files and argv is not that long. Output is JSON Lines, one object per input
// line, in input order, so the harness can stream a large run instead of buffering it.

using System.Reflection;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace TaglibProbe;

internal static class Program
{
    private static int Main(string[] args)
    {
        Console.OutputEncoding = new UTF8Encoding(false);

        if (args.Contains("--version"))
        {
            Console.WriteLine(TagLibVersion());
            return 0;
        }

        var options = new JsonSerializerOptions { WriteIndented = false };

        // A banner line so a captured run records which TagLib# actually produced it. The
        // harness reads it and refuses to compare a run whose build it cannot name.
        var banner = new JsonObject
        {
            ["kind"] = "banner",
            ["taglib_version"] = TagLibVersion(),
            ["runtime"] = Environment.Version.ToString(),
        };
        Console.WriteLine(banner.ToJsonString(options));

        string? line;
        while ((line = Console.In.ReadLine()) is not null)
        {
            var path = line.Trim();
            if (path.Length == 0)
            {
                continue;
            }

            Console.WriteLine(Describe(path).ToJsonString(options));
        }

        Console.Out.Flush();
        return 0;
    }

    private static string TagLibVersion()
    {
        var assembly = typeof(TagLib.File).Assembly;
        var informational = assembly
            .GetCustomAttribute<AssemblyInformationalVersionAttribute>()?.InformationalVersion;
        return informational ?? assembly.GetName().Version?.ToString() ?? "unknown";
    }

    private static JsonObject Describe(string path)
    {
        var result = new JsonObject
        {
            ["kind"] = "file",
            ["path"] = path,
        };

        TagLib.File? file = null;
        try
        {
            file = TagLib.File.Create(path);
        }
        catch (Exception ex)
        {
            // A read failure is a result, not a crash: "TagLib# cannot open this file at all"
            // is one of the outcomes the parity question is asking about, and swallowing it
            // into a skipped row would flatter TagLib#.
            result["ok"] = false;
            result["error"] = $"{ex.GetType().Name}: {ex.Message}";
            return result;
        }

        try
        {
            var properties = file.Properties;
            var tag = file.Tag;

            result["ok"] = true;
            result["mime_type"] = file.MimeType;
            result["possibly_corrupt"] = file.PossiblyCorrupt;

            if (properties is null)
            {
                result["properties"] = null;
            }
            else
            {
                result["properties"] = new JsonObject
                {
                    // TotalSeconds, not the formatted TimeSpan: the comparison needs the
                    // resolution TagLib# actually has, and rounding here would manufacture
                    // an agreement that the underlying value does not support.
                    ["duration_seconds"] = properties.Duration.TotalSeconds,
                    // TagLib# reports kbit/s here. ffprobe reports bit/s. That unit gap is
                    // the single most consequential detail in this whole measurement, and it
                    // is left un-reconciled on purpose so the harness has to state it.
                    ["audio_bitrate_kbps"] = properties.AudioBitrate,
                    ["audio_sample_rate_hz"] = properties.AudioSampleRate,
                    ["audio_channels"] = properties.AudioChannels,
                    ["bits_per_sample"] = properties.BitsPerSample,
                    ["description"] = properties.Description,
                    ["codecs"] = new JsonArray(properties.Codecs
                        .Where(c => c is not null)
                        .Select(c => (JsonNode)JsonValue.Create(c!.Description ?? string.Empty)!)
                        .ToArray()),
                };
            }

            if (tag is null)
            {
                result["tag"] = null;
            }
            else
            {
                result["tag"] = new JsonObject
                {
                    ["title"] = tag.Title,
                    ["performers"] = Strings(tag.Performers),
                    ["album"] = tag.Album,
                    ["album_artists"] = Strings(tag.AlbumArtists),
                    ["composers"] = Strings(tag.Composers),
                    ["genres"] = Strings(tag.Genres),
                    ["track"] = tag.Track,
                    ["track_count"] = tag.TrackCount,
                    ["disc"] = tag.Disc,
                    ["disc_count"] = tag.DiscCount,
                    ["year"] = tag.Year,
                    ["comment"] = tag.Comment,
                    ["tag_types"] = tag.TagTypes.ToString(),
                };
            }
        }
        catch (Exception ex)
        {
            result["ok"] = false;
            result["error"] = $"{ex.GetType().Name}: {ex.Message}";
        }
        finally
        {
            file.Dispose();
        }

        return result;
    }

    private static JsonArray Strings(IEnumerable<string>? values)
    {
        var array = new JsonArray();
        foreach (var value in values ?? Enumerable.Empty<string>())
        {
            array.Add(JsonValue.Create(value ?? string.Empty));
        }

        return array;
    }
}
