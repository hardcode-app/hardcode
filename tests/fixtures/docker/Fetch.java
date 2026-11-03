/**
 * GET a URL over HTTPS with Java's default trust store (cacerts) and print
 * the status, or the error. The Temurin containers of tests/docker.py run
 * it with the JDK's source launcher:
 *
 *     java Fetch.java URL
 */
public class Fetch {
    /** Fetch args[0]; exit 1 when it can't be fetched. */
    public static void main(String[] args) {
        try {
            var connection = (java.net.HttpURLConnection) new java.net.URI(args[0]).toURL().openConnection();
            connection.setConnectTimeout(10000);
            connection.setReadTimeout(10000);
            System.out.println("status " + connection.getResponseCode());
        } catch (Exception error) {
            System.out.println("failed: " + error);
            System.exit(1);
        }
    }
}
